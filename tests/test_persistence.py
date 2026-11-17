import contextlib
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

import guard
import service


def person(owner=False, state='look', score=.1):
    feature = np.zeros(128, np.float32)
    feature[0] = 1
    return guard.Observation((10, 20, 30, 40), feature, state, score, owner)


class SnapshotTests(unittest.TestCase):
    def test_own_face_never_saved_even_after_five_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = guard.SnapshotRecorder(Path(directory))
            track = guard.Track(1, (10,20,30,40), person().feature, 0, seconds=20, owner=True)
            result = writer.maybe_save(np.zeros((100,100,3),np.uint8), person(owner=True, score=.9), track)
            self.assertIsNone(result)
            self.assertFalse(list(Path(directory).rglob('*.jpg')))

    def test_five_seconds_is_not_enough_but_over_five_saves_once(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = guard.SnapshotRecorder(Path(directory))
            frame = np.full((100,100,3), 120, np.uint8)
            track = guard.Track(1, (10,20,30,40), person().feature, 0, seconds=5)
            self.assertIsNone(writer.maybe_save(frame, person(), track))
            track.seconds = 5.2
            result = writer.maybe_save(frame, person(), track)
            self.assertTrue(result.exists())
            self.assertEqual(cv2.imread(str(result)).shape[:2], (40,30))
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result.parent.stat().st_mode & 0o777, 0o700)
            metadata = json.loads(result.with_suffix('.json').read_text())
            self.assertEqual(metadata['gaze_seconds'], 5.2)
            self.assertTrue(metadata['classification_is_estimate'])
            self.assertIsNone(writer.maybe_save(frame, person(), track))
            self.assertEqual(len(list(Path(directory).rglob('*.jpg'))), 1)

    def test_away_blink_unknown_and_ambiguous_identity_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = guard.SnapshotRecorder(Path(directory))
            frame = np.zeros((100,100,3),np.uint8)
            for state in ('away', 'blink', 'unknown'):
                track = guard.Track(1, (10,20,30,40), person().feature, 0, seconds=10)
                self.assertIsNone(writer.maybe_save(frame, person(state=state), track))
            track = guard.Track(1, (10,20,30,40), person().feature, 0, seconds=10,
                                run_max_owner_score=.34)
            self.assertIsNone(writer.maybe_save(frame, person(score=.1), track))
            self.assertFalse(list(Path(directory).rglob('*.jpg')))

    def test_looking_away_allows_a_new_photo_after_new_full_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = guard.SnapshotRecorder(Path(directory))
            counter = guard.WatchCounter()
            frame = np.zeros((100,100,3),np.uint8)
            for step in range(7):
                tracks, _ = counter.update([person()], step)
                writer.maybe_save(frame, person(), tracks[0])
            self.assertEqual(len(list(Path(directory).rglob('*.jpg'))), 1)
            counter.update([person(state='away')], 7)
            for step in range(8,15):
                tracks, _ = counter.update([person()], step)
                writer.maybe_save(frame, person(), tracks[0])
            self.assertEqual(len(list(Path(directory).rglob('*.jpg'))), 2)

    def test_ambiguous_identity_anytime_in_gaze_event_prevents_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            writer, counter = guard.SnapshotRecorder(Path(directory)), guard.WatchCounter()
            frame = np.zeros((100,100,3),np.uint8)
            for step in range(9):
                obs = person(score=.34 if step==0 else .1)
                tracks, _ = counter.update([obs], step)
                writer.maybe_save(frame, obs, tracks[0])
            self.assertFalse(list(Path(directory).rglob('*.jpg')))

    def test_single_person_simulation_cannot_enable_photos(self):
        p = subprocess.run([sys.executable, str(guard.ROOT/'guard.py'), 'watch',
                            '--simulate-self', '--save-snapshots'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn('单人模拟不保存照片', p.stderr)


class ServiceConfigurationTests(unittest.TestCase):
    def test_cleanup_excludes_unrelated_processes_and_shell_commands(self):
        listing = (f'101 /opt/python -u {service.APP}/guard.py watch --lock\n'
                   f'102 /opt/python -u {service.APP}/service.py run\n'
                   f'103 /bin/zsh -lc {service.APP}/guard.py watch\n'
                   '104 /opt/python /some/other/guard.py watch\n')
        def ps(args, **kwargs):
            executable = '/bin/zsh\n' if args[2]=='103' else '/opt/python\n'
            return SimpleNamespace(returncode=0, stdout=executable)
        with (patch.object(service.subprocess, 'check_output', return_value=listing),
              patch.object(service.subprocess, 'run', side_effect=ps)):
            self.assertEqual(set(service.owned_processes()), {101,102})

    def test_disabled_status_handles_current_and_older_macos_formats(self):
        self.assertTrue(service.is_disabled(f'"{service.LABEL}" => disabled'))
        self.assertTrue(service.is_disabled(f'"{service.LABEL}" => true'))
        self.assertFalse(service.is_disabled(f'"{service.LABEL}" => enabled'))
        self.assertFalse(service.is_disabled('"another.job" => disabled'))

    def test_service_is_independent_of_chat_and_restarts_after_exit(self):
        config = service.configuration()
        self.assertIs(config['KeepAlive'], True)
        self.assertIs(config['RunAtLoad'], True)
        self.assertEqual(config['LimitLoadToSessionType'], 'Aqua')
        self.assertEqual(config['WorkingDirectory'], str(service.APP))
        self.assertTrue(all('Documents/ChatGPT' not in p for p in config['ProgramArguments']))


class CameraRecoveryTests(unittest.TestCase):
    def test_camera_disconnect_retries_instead_of_exiting(self):
        args = SimpleNamespace(min_face=65, owner_threshold=.363, seconds=15., fps=5.,
                               idle_fps=1., lock=False, simulate_self=False, no_preview=False,
                               camera=0, save_snapshots=False, status_file=None)
        cap1, cap2, detector, locker = Mock(), Mock(), Mock(), Mock()
        locker.locked.return_value = False
        detector.observe.return_value = []
        with (patch.object(guard,'load_owner',return_value=np.stack([person().feature])),
              patch.object(guard,'MacLock',return_value=locker),
              patch.object(guard,'Vision',return_value=detector),
              patch.object(guard,'camera',side_effect=[cap1,cap2]),
              patch.object(guard,'read_frame',side_effect=[RuntimeError('camera disconnected'),None]),
              patch.object(guard,'show',return_value=ord('q')),
              patch.object(guard.cv2,'destroyAllWindows'),
              patch.object(guard.time,'sleep'),
              contextlib.redirect_stdout(io.StringIO())):
            guard.watch(args)
        cap1.release.assert_called_once()
        cap2.release.assert_called_once()
        locker.lock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
