import contextlib
import hashlib
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import service


@contextlib.contextmanager
def isolated_deployment():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        source, app = base/'source', base/'runtime'
        for folder in (source/'.private', source/'models', app/'.venv/bin'):
            folder.mkdir(parents=True)
        for name in ('guard.py','setup_models.py','service.py','CameraHost.swift'):
            (source/name).write_text('# synthetic install test input\n')
        requirements = b'# no dependencies in isolated test\n'
        (source/'requirements.txt').write_bytes(requirements)
        (source/'.private/owner.npz').write_bytes(b'synthetic profile, not a real face')
        (source/'models/test-model.onnx').write_bytes(b'synthetic model placeholder')
        (app/'.venv/bin/python').touch()
        (app/'.requirements-hash').write_text(hashlib.sha256(requirements).hexdigest())
        bundle = app/'ScreenPrivacyGuard.app'
        values = dict(SOURCE=source, APP=app, STATE=app/'state', CAPTURES=base/'captures',
                      PLIST=base/'LaunchAgents/test.plist', BUNDLE=bundle,
                      HOST=bundle/'Contents/MacOS/ScreenPrivacyGuard')
        def process(args, **kwargs):
            if args[0]=='/usr/bin/xcrun':
                values['HOST'].touch()
            return SimpleNamespace(returncode=0)
        with (patch.multiple(service, **values),
              patch.object(service.subprocess, 'run', side_effect=process) as calls,
              patch.object(service, 'ctl', side_effect=AssertionError('deployment must not contact launchd'))):
            yield values, calls


class InstallationTests(unittest.TestCase):
    def test_prepare_without_registering_does_not_enable_login_task(self):
        with isolated_deployment() as (paths, calls):
            service.deploy(register_agent=False)
            self.assertFalse(paths['PLIST'].exists())
            self.assertTrue((paths['APP']/'guard.py').exists())
            self.assertTrue(paths['HOST'].exists())
            self.assertEqual((paths['APP']/'.private/owner.npz').stat().st_mode & 0o777, 0o600)
            self.assertFalse(any('launchctl' in str(call.args) for call in calls.call_args_list))

    def test_registered_configuration_is_written_without_loading_it(self):
        with isolated_deployment() as (paths, _):
            service.deploy(register_agent=True)
            config = plistlib.loads(paths['PLIST'].read_bytes())
            self.assertIs(config['KeepAlive'], True)
            self.assertIs(config['RunAtLoad'], True)

    def test_camera_host_is_not_recompiled_when_unchanged(self):
        with isolated_deployment() as (_, calls):
            service.deploy(register_agent=False)
            initial = calls.call_count
            service.deploy(register_agent=False)
            self.assertEqual(calls.call_count, initial)


if __name__ == '__main__':
    unittest.main()
