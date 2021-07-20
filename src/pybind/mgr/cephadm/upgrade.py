import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Optional, Dict, List, Tuple

import orchestrator
from cephadm.serve import CephadmServe
from cephadm.services.cephadmservice import CephadmDaemonDeploySpec
from cephadm.utils import ceph_release_to_major, name_to_config_section, CEPH_UPGRADE_ORDER, MONITORING_STACK_TYPES
from orchestrator import OrchestratorError, DaemonDescription, DaemonDescriptionStatus, daemon_type_to_service

if TYPE_CHECKING:
    from .module import CephadmOrchestrator


logger = logging.getLogger(__name__)


def normalize_image_digest(digest: str, default_registry: str) -> str:
    # normal case:
    #   ceph/ceph -> docker.io/ceph/ceph
    # edge cases that shouldn't ever come up:
    #   ubuntu -> docker.io/ubuntu    (ubuntu alias for library/ubuntu)
    # no change:
    #   quay.ceph.io/ceph/ceph -> ceph
    #   docker.io/ubuntu -> no change
    bits = digest.split('/')
    if '.' not in bits[0] or len(bits) < 3:
        digest = 'docker.io/' + digest
    return digest


class UpgradeState:
    def __init__(self,
                 target_name: str,
                 progress_id: str,
                 target_id: Optional[str] = None,
                 target_digests: Optional[List[str]] = None,
                 target_version: Optional[str] = None,
                 error: Optional[str] = None,
                 paused: Optional[bool] = None,
                 fs_original_max_mds: Optional[Dict[str, int]] = None,
                 ):
        self._target_name: str = target_name  # Use CephadmUpgrade.target_image instead.
        self.progress_id: str = progress_id
        self.target_id: Optional[str] = target_id
        self.target_digests: Optional[List[str]] = target_digests
        self.target_version: Optional[str] = target_version
        self.error: Optional[str] = error
        self.paused: bool = paused or False
        self.fs_original_max_mds: Optional[Dict[str, int]] = fs_original_max_mds

    def to_json(self) -> dict:
        return {
            'target_name': self._target_name,
            'progress_id': self.progress_id,
            'target_id': self.target_id,
            'target_digests': self.target_digests,
            'target_version': self.target_version,
            'fs_original_max_mds': self.fs_original_max_mds,
            'error': self.error,
            'paused': self.paused,
        }

    @classmethod
    def from_json(cls, data: dict) -> Optional['UpgradeState']:
        if data:
            c = {k: v for k, v in data.items()}
            if 'repo_digest' in c:
                c['target_digests'] = [c.pop('repo_digest')]
            return cls(**c)
        else:
            return None


class CephadmUpgrade:
    UPGRADE_ERRORS = [
        'UPGRADE_NO_STANDBY_MGR',
        'UPGRADE_FAILED_PULL',
        'UPGRADE_REDEPLOY_DAEMON',
        'UPGRADE_BAD_TARGET_VERSION',
        'UPGRADE_EXCEPTION'
    ]

    def __init__(self, mgr: "CephadmOrchestrator"):
        self.mgr = mgr

        t = self.mgr.get_store('upgrade_state')
        if t:
            self.upgrade_state: Optional[UpgradeState] = UpgradeState.from_json(json.loads(t))
        else:
            self.upgrade_state = None

    @property
    def target_image(self) -> str:
        assert self.upgrade_state
        if not self.mgr.use_repo_digest:
            return self.upgrade_state._target_name
        if not self.upgrade_state.target_digests:
            return self.upgrade_state._target_name

        # FIXME: we assume the first digest is the best one to use
        return self.upgrade_state.target_digests[0]

    def upgrade_status(self) -> orchestrator.UpgradeStatusSpec:
        r = orchestrator.UpgradeStatusSpec()
        if self.upgrade_state:
            r.target_image = self.target_image
            r.in_progress = True
            r.progress, r.services_complete = self._get_upgrade_info()
            # accessing self.upgrade_info_str will throw an exception if it
            # has not been set in _do_upgrade yet
            try:
                r.message = self.upgrade_info_str
            except AttributeError:
                pass
            if self.upgrade_state.error:
                r.message = 'Error: ' + self.upgrade_state.error
            elif self.upgrade_state.paused:
                r.message = 'Upgrade paused'
        return r

    def _get_upgrade_info(self) -> Tuple[str, List[str]]:
        if not self.upgrade_state or not self.upgrade_state.target_digests:
            return '', []

        daemons = [d for d in self.mgr.cache.get_daemons() if d.daemon_type in CEPH_UPGRADE_ORDER]

        if any(not d.container_image_digests for d in daemons if d.daemon_type == 'mgr'):
            return '', []

        completed_daemons = [(d.daemon_type, any(d in self.upgrade_state.target_digests for d in (
            d.container_image_digests or []))) for d in daemons if d.daemon_type]

        done = len([True for completion in completed_daemons if completion[1]])

        completed_types = list(set([completion[0] for completion in completed_daemons if all(
            c[1] for c in completed_daemons if c[0] == completion[0])]))

        return '%s/%s ceph daemons upgraded' % (done, len(daemons)), completed_types

    def _check_target_version(self, version: str) -> Optional[str]:
        try:
            (major, minor, _) = version.split('.', 2)
            assert int(minor) >= 0
            # patch might be a number or {number}-g{sha1}
        except ValueError:
            return 'version must be in the form X.Y.Z (e.g., 15.2.3)'
        if int(major) < 15 or (int(major) == 15 and int(minor) < 2):
            return 'cephadm only supports octopus (15.2.0) or later'

        # to far a jump?
        current_version = self.mgr.version.split('ceph version ')[1]
        (current_major, current_minor, _) = current_version.split('-')[0].split('.', 2)
        if int(current_major) < int(major) - 2:
            return f'ceph can only upgrade 1 or 2 major versions at a time; {current_version} -> {version} is too big a jump'
        if int(current_major) > int(major):
            return f'ceph cannot downgrade major versions (from {current_version} to {version})'
        if int(current_major) == int(major):
            if int(current_minor) > int(minor):
                return f'ceph cannot downgrade to a {"rc" if minor == "1" else "dev"} release'

        # check mon min
        monmap = self.mgr.get("mon_map")
        mon_min = monmap.get("min_mon_release", 0)
        if mon_min < int(major) - 2:
            return f'min_mon_release ({mon_min}) < target {major} - 2; first complete an upgrade to an earlier release'

        # check osd min
        osdmap = self.mgr.get("osd_map")
        osd_min_name = osdmap.get("require_osd_release", "argonaut")
        osd_min = ceph_release_to_major(osd_min_name)
        if osd_min < int(major) - 2:
            return f'require_osd_release ({osd_min_name} or {osd_min}) < target {major} - 2; first complete an upgrade to an earlier release'

        return None

    def upgrade_start(self, image: str, version: str) -> str:
        if self.mgr.mode != 'root':
            raise OrchestratorError('upgrade is not supported in %s mode' % (
                self.mgr.mode))
        if version:
            version_error = self._check_target_version(version)
            if version_error:
                raise OrchestratorError(version_error)
            target_name = self.mgr.container_image_base + ':v' + version
        elif image:
            target_name = normalize_image_digest(image, self.mgr.default_registry)
        else:
            raise OrchestratorError('must specify either image or version')
        if self.upgrade_state:
            if self.upgrade_state._target_name != target_name:
                raise OrchestratorError(
                    'Upgrade to %s (not %s) already in progress' %
                    (self.upgrade_state._target_name, target_name))
            if self.upgrade_state.paused:
                self.upgrade_state.paused = False
                self._save_upgrade_state()
                return 'Resumed upgrade to %s' % self.target_image
            return 'Upgrade to %s in progress' % self.target_image

        running_mgr_count = len([daemon for daemon in self.mgr.cache.get_daemons_by_type(
            'mgr') if daemon.status == DaemonDescriptionStatus.running])

        if running_mgr_count < 2:
            raise OrchestratorError('Need at least 2 running mgr daemons for upgrade')

        self.mgr.log.info('Upgrade: Started with target %s' % target_name)
        self.upgrade_state = UpgradeState(
            target_name=target_name,
            progress_id=str(uuid.uuid4())
        )
        self._update_upgrade_progress(0.0)
        self._save_upgrade_state()
        self._clear_upgrade_health_checks()
        self.mgr.event.set()
        return 'Initiating upgrade to %s' % (target_name)

    def upgrade_pause(self) -> str:
        if not self.upgrade_state:
            raise OrchestratorError('No upgrade in progress')
        if self.upgrade_state.paused:
            return 'Upgrade to %s already paused' % self.target_image
        self.upgrade_state.paused = True
        self.mgr.log.info('Upgrade: Paused upgrade to %s' % self.target_image)
        self._save_upgrade_state()
        return 'Paused upgrade to %s' % self.target_image

    def upgrade_resume(self) -> str:
        if not self.upgrade_state:
            raise OrchestratorError('No upgrade in progress')
        if not self.upgrade_state.paused:
            return 'Upgrade to %s not paused' % self.target_image
        self.upgrade_state.paused = False
        self.mgr.log.info('Upgrade: Resumed upgrade to %s' % self.target_image)
        self._save_upgrade_state()
        self.mgr.event.set()
        return 'Resumed upgrade to %s' % self.target_image

    def upgrade_stop(self) -> str:
        if not self.upgrade_state:
            return 'No upgrade in progress'
        if self.upgrade_state.progress_id:
            self.mgr.remote('progress', 'complete',
                            self.upgrade_state.progress_id)
        target_image = self.target_image
        self.mgr.log.info('Upgrade: Stopped')
        self.upgrade_state = None
        self._save_upgrade_state()
        self._clear_upgrade_health_checks()
        self.mgr.event.set()
        return 'Stopped upgrade to %s' % target_image

    def continue_upgrade(self) -> bool:
        """
        Returns false, if nothing was done.
        :return:
        """
        if self.upgrade_state and not self.upgrade_state.paused:
            try:
                self._do_upgrade()
            except Exception as e:
                self._fail_upgrade('UPGRADE_EXCEPTION', {
                    'severity': 'error',
                    'summary': 'Upgrade: failed due to an unexpected exception',
                    'count': 1,
                    'detail': [f'Unexpected exception occurred during upgrade process: {str(e)}'],
                })
                return False
            return True
        return False

    def _wait_for_ok_to_stop(
            self, s: DaemonDescription,
            known: Optional[List[str]] = None,  # NOTE: output argument!
    ) -> bool:
        # only wait a little bit; the service might go away for something
        assert s.daemon_type is not None
        assert s.daemon_id is not None
        tries = 4
        while tries > 0:
            if not self.upgrade_state or self.upgrade_state.paused:
                return False

            # setting force flag to retain old functionality.
            # note that known is an output argument for ok_to_stop()
            r = self.mgr.cephadm_services[daemon_type_to_service(s.daemon_type)].ok_to_stop([
                s.daemon_id], known=known, force=True)

            if not r.retval:
                logger.info(f'Upgrade: {r.stdout}')
                return True
            logger.info(f'Upgrade: {r.stderr}')

            time.sleep(15)
            tries -= 1
        return False

    def _clear_upgrade_health_checks(self) -> None:
        for k in self.UPGRADE_ERRORS:
            if k in self.mgr.health_checks:
                del self.mgr.health_checks[k]
        self.mgr.set_health_checks(self.mgr.health_checks)

    def _fail_upgrade(self, alert_id: str, alert: dict) -> None:
        assert alert_id in self.UPGRADE_ERRORS
        if not self.upgrade_state:
            # this could happen if the user canceled the upgrade while we
            # were doing something
            return

        logger.error('Upgrade: Paused due to %s: %s' % (alert_id,
                                                        alert['summary']))
        self.upgrade_state.error = alert_id + ': ' + alert['summary']
        self.upgrade_state.paused = True
        self._save_upgrade_state()
        self.mgr.health_checks[alert_id] = alert
        self.mgr.set_health_checks(self.mgr.health_checks)

    def _update_upgrade_progress(self, progress: float) -> None:
        if not self.upgrade_state:
            assert False, 'No upgrade in progress'

        if not self.upgrade_state.progress_id:
            self.upgrade_state.progress_id = str(uuid.uuid4())
            self._save_upgrade_state()
        self.mgr.remote('progress', 'update', self.upgrade_state.progress_id,
                        ev_msg='Upgrade to %s' % (
                            self.upgrade_state.target_version or self.target_image
                        ),
                        ev_progress=progress,
                        add_to_ceph_s=True)

    def _save_upgrade_state(self) -> None:
        if not self.upgrade_state:
            self.mgr.set_store('upgrade_state', None)
            return
        self.mgr.set_store('upgrade_state', json.dumps(self.upgrade_state.to_json()))

    def get_distinct_container_image_settings(self) -> Dict[str, str]:
        # get all distinct container_image settings
        image_settings = {}
        ret, out, err = self.mgr.check_mon_command({
            'prefix': 'config dump',
            'format': 'json',
        })
        config = json.loads(out)
        for opt in config:
            if opt['name'] == 'container_image':
                image_settings[opt['section']] = opt['value']
        return image_settings

    def _prepare_for_mds_upgrade(
        self,
        target_major: str,
        need_upgrade: List[DaemonDescription]
    ) -> bool:
        # are any daemons running a different major version?
        scale_down = False
        for name, info in self.mgr.get("mds_metadata").items():
            version = info.get("ceph_version_short")
            major_version = None
            if version:
                major_version = version.split('.')[0]
            if not major_version:
                self.mgr.log.info('Upgrade: mds.%s version is not known, will retry' % name)
                time.sleep(5)
                return False
            if int(major_version) < int(target_major):
                scale_down = True

        if not scale_down:
            self.mgr.log.debug('Upgrade: All MDS daemons run same major version')
            return True

        # scale down all filesystems to 1 MDS
        assert self.upgrade_state
        if not self.upgrade_state.fs_original_max_mds:
            self.upgrade_state.fs_original_max_mds = {}
        fsmap = self.mgr.get("fs_map")
        continue_upgrade = True
        for i in fsmap.get('filesystems', []):
            fs = i["mdsmap"]
            fs_id = i["id"]
            fs_name = fs["fs_name"]

            # scale down this filesystem?
            if fs["max_mds"] > 1:
                self.mgr.log.info('Upgrade: Scaling down filesystem %s' % (
                    fs_name
                ))
                if fs_id not in self.upgrade_state.fs_original_max_mds:
                    self.upgrade_state.fs_original_max_mds[fs_id] = fs['max_mds']
                    self._save_upgrade_state()
                ret, out, err = self.mgr.check_mon_command({
                    'prefix': 'fs set',
                    'fs_name': fs_name,
                    'var': 'max_mds',
                    'val': '1',
                })
                continue_upgrade = False
                continue

            if len(fs['info']) > 1:
                self.mgr.log.info('Upgrade: Waiting for fs %s to scale down to 1 MDS' % (fs_name))
                time.sleep(10)
                continue_upgrade = False
                continue

            lone_mds = list(fs['info'].values())[0]
            if lone_mds['state'] != 'up:active':
                self.mgr.log.info('Upgrade: Waiting for mds.%s to be up:active (currently %s)' % (
                    lone_mds['name'],
                    lone_mds['state'],
                ))
                time.sleep(10)
                continue_upgrade = False
                continue

        return continue_upgrade

    def _enough_mons_for_ok_to_stop(self) -> bool:
        # type () -> bool
        ret, out, err = self.mgr.check_mon_command({
            'prefix': 'quorum_status',
        })
        try:
            j = json.loads(out)
        except Exception:
            raise OrchestratorError('failed to parse quorum status')

        mons = [m['name'] for m in j['monmap']['mons']]
        return len(mons) > 2

    def _enough_mds_for_ok_to_stop(self, mds_daemon: DaemonDescription) -> bool:
        # type (DaemonDescription) -> bool

        # find fs this mds daemon belongs to
        fsmap = self.mgr.get("fs_map")
        for i in fsmap.get('filesystems', []):
            fs = i["mdsmap"]
            fs_name = fs["fs_name"]

            assert mds_daemon.daemon_id
            if fs_name != mds_daemon.service_name().split('.', 1)[1]:
                # wrong fs for this mds daemon
                continue

            # get number of mds daemons for this fs
            mds_count = len(
                [daemon for daemon in self.mgr.cache.get_daemons_by_service(mds_daemon.service_name())])

            # standby mds daemons for this fs?
            if fs["max_mds"] < mds_count:
                return True
            return False

        return True  # if mds has no fs it should pass ok-to-stop

    def _do_upgrade(self):
        # type: () -> None
        if not self.upgrade_state:
            logger.debug('_do_upgrade no state, exiting')
            return

        target_image = self.target_image
        target_id = self.upgrade_state.target_id
        target_digests = self.upgrade_state.target_digests
        target_version = self.upgrade_state.target_version

        first = False
        if not target_id or not target_version or not target_digests:
            # need to learn the container hash
            logger.info('Upgrade: First pull of %s' % target_image)
            self.upgrade_info_str = 'Doing first pull of %s image' % (target_image)
            try:
                target_id, target_version, target_digests = CephadmServe(self.mgr)._get_container_image_info(
                    target_image)
            except OrchestratorError as e:
                self._fail_upgrade('UPGRADE_FAILED_PULL', {
                    'severity': 'warning',
                    'summary': 'Upgrade: failed to pull target image',
                    'count': 1,
                    'detail': [str(e)],
                })
                return
            if not target_version:
                self._fail_upgrade('UPGRADE_FAILED_PULL', {
                    'severity': 'warning',
                    'summary': 'Upgrade: failed to pull target image',
                    'count': 1,
                    'detail': ['unable to extract ceph version from container'],
                })
                return
            self.upgrade_state.target_id = target_id
            # extract the version portion of 'ceph version {version} ({sha1})'
            self.upgrade_state.target_version = target_version.split(' ')[2]
            self.upgrade_state.target_digests = target_digests
            self._save_upgrade_state()
            target_image = self.target_image
            first = True

        if target_digests is None:
            target_digests = []
        if target_version.startswith('ceph version '):
            # tolerate/fix upgrade state from older version
            self.upgrade_state.target_version = target_version.split(' ')[2]
            target_version = self.upgrade_state.target_version
        (target_major, _) = target_version.split('.', 1)
        target_major_name = self.mgr.lookup_release_name(int(target_major))

        if first:
            logger.info('Upgrade: Target is version %s (%s)' % (
                target_version, target_major_name))
            logger.info('Upgrade: Target container is %s, digests %s' % (
                target_image, target_digests))

        version_error = self._check_target_version(target_version)
        if version_error:
            self._fail_upgrade('UPGRADE_BAD_TARGET_VERSION', {
                'severity': 'error',
                'summary': f'Upgrade: cannot upgrade/downgrade to {target_version}',
                'count': 1,
                'detail': [version_error],
            })
            return

        image_settings = self.get_distinct_container_image_settings()

        daemons = [d for d in self.mgr.cache.get_daemons() if d.daemon_type in CEPH_UPGRADE_ORDER]
        done = 0
        for daemon_type in CEPH_UPGRADE_ORDER:
            logger.debug('Upgrade: Checking %s daemons' % daemon_type)

            need_upgrade_self = False
            need_upgrade: List[Tuple[DaemonDescription, bool]] = []
            need_upgrade_deployer: List[Tuple[DaemonDescription, bool]] = []
            for d in daemons:
                if d.daemon_type != daemon_type:
                    continue
                assert d.daemon_type is not None
                assert d.daemon_id is not None
                correct_digest = False
                if (any(d in target_digests for d in (d.container_image_digests or []))
                        or d.daemon_type in MONITORING_STACK_TYPES):
                    logger.debug('daemon %s.%s container digest correct' % (
                        daemon_type, d.daemon_id))
                    correct_digest = True
                    if any(d in target_digests for d in (d.deployed_by or [])):
                        logger.debug('daemon %s.%s deployed by correct version' % (
                            d.daemon_type, d.daemon_id))
                        done += 1
                        continue

                if self.mgr.daemon_is_self(d.daemon_type, d.daemon_id):
                    logger.info('Upgrade: Need to upgrade myself (mgr.%s)' %
                                self.mgr.get_mgr_id())
                    need_upgrade_self = True
                    continue

                if correct_digest:
                    logger.debug('daemon %s.%s not deployed by correct version' % (
                        d.daemon_type, d.daemon_id))
                    need_upgrade_deployer.append((d, True))
                else:
                    logger.debug('daemon %s.%s not correct (%s, %s, %s)' % (
                        daemon_type, d.daemon_id,
                        d.container_image_name, d.container_image_digests, d.version))
                    need_upgrade.append((d, False))

            if not need_upgrade_self:
                # only after the mgr itself is upgraded can we expect daemons to have
                # deployed_by == target_digests
                need_upgrade += need_upgrade_deployer

            # prepare filesystems for daemon upgrades?
            if (
                daemon_type == 'mds'
                and need_upgrade
                and not self._prepare_for_mds_upgrade(target_major, [d_entry[0] for d_entry in need_upgrade])
            ):
                return

            if need_upgrade:
                self.upgrade_info_str = 'Currently upgrading %s daemons' % (daemon_type)

            to_upgrade: List[Tuple[DaemonDescription, bool]] = []
            known_ok_to_stop: List[str] = []
            for d_entry in need_upgrade:
                d = d_entry[0]
                assert d.daemon_type is not None
                assert d.daemon_id is not None
                assert d.hostname is not None

                if not d.container_image_id:
                    if d.container_image_name == target_image:
                        logger.debug(
                            'daemon %s has unknown container_image_id but has correct image name' % (d.name()))
                        continue

                if known_ok_to_stop:
                    if d.name() in known_ok_to_stop:
                        logger.info(f'Upgrade: {d.name()} is also safe to restart')
                        to_upgrade.append(d_entry)
                    continue

                if d.daemon_type == 'osd':
                    # NOTE: known_ok_to_stop is an output argument for
                    # _wait_for_ok_to_stop
                    if not self._wait_for_ok_to_stop(d, known_ok_to_stop):
                        return

                if d.daemon_type == 'mon' and self._enough_mons_for_ok_to_stop():
                    if not self._wait_for_ok_to_stop(d, known_ok_to_stop):
                        return

                if d.daemon_type == 'mds' and self._enough_mds_for_ok_to_stop(d):
                    if not self._wait_for_ok_to_stop(d, known_ok_to_stop):
                        return

                to_upgrade.append(d_entry)

                # if we don't have a list of others to consider, stop now
                if not known_ok_to_stop:
                    break

            num = 1
            for d_entry in to_upgrade:
                d = d_entry[0]
                assert d.daemon_type is not None
                assert d.daemon_id is not None
                assert d.hostname is not None

                self._update_upgrade_progress(done / len(daemons))

                # make sure host has latest container image
                out, errs, code = CephadmServe(self.mgr)._run_cephadm(
                    d.hostname, '', 'inspect-image', [],
                    image=target_image, no_fsid=True, error_ok=True)
                if code or not any(d in target_digests for d in json.loads(''.join(out)).get('repo_digests', [])):
                    logger.info('Upgrade: Pulling %s on %s' % (target_image,
                                                               d.hostname))
                    self.upgrade_info_str = 'Pulling %s image on host %s' % (
                        target_image, d.hostname)
                    out, errs, code = CephadmServe(self.mgr)._run_cephadm(
                        d.hostname, '', 'pull', [],
                        image=target_image, no_fsid=True, error_ok=True)
                    if code:
                        self._fail_upgrade('UPGRADE_FAILED_PULL', {
                            'severity': 'warning',
                            'summary': 'Upgrade: failed to pull target image',
                            'count': 1,
                            'detail': [
                                'failed to pull %s on host %s' % (target_image,
                                                                  d.hostname)],
                        })
                        return
                    r = json.loads(''.join(out))
                    if not any(d in target_digests for d in r.get('repo_digests', [])):
                        logger.info('Upgrade: image %s pull on %s got new digests %s (not %s), restarting' % (
                            target_image, d.hostname, r['repo_digests'], target_digests))
                        self.upgrade_info_str = 'Image %s pull on %s got new digests %s (not %s), restarting' % (
                            target_image, d.hostname, r['repo_digests'], target_digests)
                        self.upgrade_state.target_digests = r['repo_digests']
                        self._save_upgrade_state()
                        return

                    self.upgrade_info_str = 'Currently upgrading %s daemons' % (daemon_type)

                if len(to_upgrade) > 1:
                    logger.info('Upgrade: Updating %s.%s (%d/%d)' %
                                (d.daemon_type, d.daemon_id, num, len(to_upgrade)))
                else:
                    logger.info('Upgrade: Updating %s.%s' %
                                (d.daemon_type, d.daemon_id))
                action = 'Upgrading' if not d_entry[1] else 'Redeploying'
                try:
                    daemon_spec = CephadmDaemonDeploySpec.from_daemon_description(d)
                    self.mgr._daemon_action(
                        daemon_spec,
                        'redeploy',
                        image=target_image if not d_entry[1] else None
                    )
                except Exception as e:
                    self._fail_upgrade('UPGRADE_REDEPLOY_DAEMON', {
                        'severity': 'warning',
                        'summary': f'{action} daemon {d.name()} on host {d.hostname} failed.',
                        'count': 1,
                        'detail': [
                            f'Upgrade daemon: {d.name()}: {e}'
                        ],
                    })
                    return
                num += 1
            if to_upgrade:
                return

            # complete mon upgrade?
            if daemon_type == 'mon':
                if not self.mgr.get("have_local_config_map"):
                    logger.info('Upgrade: Restarting mgr now that mons are running pacific')
                    need_upgrade_self = True

            if need_upgrade_self:
                try:
                    self.mgr.mgr_service.fail_over()
                except OrchestratorError as e:
                    self._fail_upgrade('UPGRADE_NO_STANDBY_MGR', {
                        'severity': 'warning',
                        'summary': f'Upgrade: {e}',
                        'count': 1,
                        'detail': [
                            'The upgrade process needs to upgrade the mgr, '
                            'but it needs at least one standby to proceed.',
                        ],
                    })
                    return

                return  # unreachable code, as fail_over never returns
            elif daemon_type == 'mgr':
                if 'UPGRADE_NO_STANDBY_MGR' in self.mgr.health_checks:
                    del self.mgr.health_checks['UPGRADE_NO_STANDBY_MGR']
                    self.mgr.set_health_checks(self.mgr.health_checks)

            # make sure 'ceph versions' agrees
            ret, out_ver, err = self.mgr.check_mon_command({
                'prefix': 'versions',
            })
            j = json.loads(out_ver)
            for version, count in j.get(daemon_type, {}).items():
                short_version = version.split(' ')[2]
                if short_version != target_version:
                    logger.warning(
                        'Upgrade: %d %s daemon(s) are %s != target %s' %
                        (count, daemon_type, short_version, target_version))

            # push down configs
            daemon_type_section = name_to_config_section(daemon_type)
            if image_settings.get(daemon_type_section) != target_image:
                logger.info('Upgrade: Setting container_image for all %s' %
                            daemon_type)
                self.mgr.set_container_image(daemon_type_section, target_image)
            to_clean = []
            for section in image_settings.keys():
                if section.startswith(name_to_config_section(daemon_type) + '.'):
                    to_clean.append(section)
            if to_clean:
                logger.debug('Upgrade: Cleaning up container_image for %s' %
                             to_clean)
                for section in to_clean:
                    ret, image, err = self.mgr.check_mon_command({
                        'prefix': 'config rm',
                        'name': 'container_image',
                        'who': section,
                    })

            logger.debug('Upgrade: All %s daemons are up to date.' % daemon_type)

            # complete osd upgrade?
            if daemon_type == 'osd':
                osdmap = self.mgr.get("osd_map")
                osd_min_name = osdmap.get("require_osd_release", "argonaut")
                osd_min = ceph_release_to_major(osd_min_name)
                if osd_min < int(target_major):
                    logger.info(
                        f'Upgrade: Setting require_osd_release to {target_major} {target_major_name}')
                    ret, _, err = self.mgr.check_mon_command({
                        'prefix': 'osd require-osd-release',
                        'release': target_major_name,
                    })

            # complete mds upgrade?
            if daemon_type == 'mds' and self.upgrade_state.fs_original_max_mds:
                for i in self.mgr.get("fs_map")['filesystems']:
                    fs_id = i["id"]
                    fs_name = i['mdsmap']['fs_name']
                    new_max = self.upgrade_state.fs_original_max_mds.get(fs_id)
                    if new_max:
                        self.mgr.log.info('Upgrade: Scaling up filesystem %s max_mds to %d' % (
                            fs_name, new_max
                        ))
                        ret, _, err = self.mgr.check_mon_command({
                            'prefix': 'fs set',
                            'fs_name': fs_name,
                            'var': 'max_mds',
                            'val': str(new_max),
                        })

                self.upgrade_state.fs_original_max_mds = {}
                self._save_upgrade_state()

        # clean up
        logger.info('Upgrade: Finalizing container_image settings')
        self.mgr.set_container_image('global', target_image)

        for daemon_type in CEPH_UPGRADE_ORDER:
            ret, image, err = self.mgr.check_mon_command({
                'prefix': 'config rm',
                'name': 'container_image',
                'who': name_to_config_section(daemon_type),
            })

        logger.info('Upgrade: Complete!')
        if self.upgrade_state.progress_id:
            self.mgr.remote('progress', 'complete',
                            self.upgrade_state.progress_id)
        self.upgrade_state = None
        self._save_upgrade_state()
        return
