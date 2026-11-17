import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import guard
from guard import Observation, WatchCounter, unit


def person(which=0, state='look', owner=False, x=100):
    f = np.zeros(128, dtype=np.float32)
    f[which] = 1
    return Observation((x, 100, 100, 130), f, state, owner=owner)


def tick(counter, start, stop, *people):
    triggers = []
    for step in range(start, stop + 1):
        _, events = counter.update(list(people), float(step))
        triggers.extend(events)
    return triggers


class AdaptiveRateTests(unittest.TestCase):
    def test_empty_or_owner_only_stays_at_one_check_per_second(self):
        rate = guard.AdaptiveRate()
        self.assertEqual(rate.update([], 0), 1)
        self.assertEqual(rate.update([person(owner=True)], 1), 1)
        self.assertEqual(rate.update([person(owner=True)], 60), 1)

    def test_other_person_raises_frequency_even_when_not_looking(self):
        rate = guard.AdaptiveRate()
        self.assertEqual(rate.update([person(owner=True), person(1, state='away')], 1), 5)

    def test_drops_to_idle_two_seconds_after_other_person_disappears(self):
        rate = guard.AdaptiveRate()
        rate.update([person()], 10)
        self.assertEqual(rate.update([], 10.2), 5)
        self.assertEqual(rate.update([person(owner=True)], 11.9), 5)
        self.assertEqual(rate.update([person(owner=True)], 12), 1)

    def test_reset_returns_to_idle(self):
        rate = guard.AdaptiveRate()
        rate.update([person()], 0)
        rate.reset()
        self.assertEqual(rate.update([], .2), 1)

    def test_presence_alone_does_not_start_fifteen_second_gaze_timer(self):
        rate, counter = guard.AdaptiveRate(), WatchCounter()
        for i in range(120):
            now = i * .2
            observations = [person(state='away')]
            self.assertEqual(rate.update(observations, now), 5)
            tracks, triggers = counter.update(observations, now)
            self.assertEqual(tracks[0].seconds, 0)
            self.assertFalse(triggers)

    def test_first_detection_switches_to_fast_checks_and_waits_full_fifteen_seconds(self):
        rate, counter = guard.AdaptiveRate(), WatchCounter()
        now, first_other, fired_at = 0., None, None
        while now < 30:
            observations = [person(owner=True)] if now < 10 else [person(1)]
            if now >= 10 and first_other is None:
                first_other = now
            _, triggers = counter.update(observations, now)
            fps = rate.update(observations, now)
            if triggers:
                fired_at = now
                break
            now = round(now + 1/fps, 6)
        self.assertIsNotNone(fired_at)
        self.assertGreaterEqual(fired_at - first_other, 15)
        self.assertLessEqual(fired_at - first_other, 15.21)

    def test_watch_loop_actually_uses_the_selected_frequency(self):
        clock = SimpleNamespace(now=0., sleeps=[])
        def sleep(seconds):
            clock.sleeps.append(seconds)
            clock.now += seconds
        detector, locker, cap = Mock(), Mock(), Mock()
        locker.locked.return_value = False
        detector.observe.side_effect = [[person(owner=True)], [person(owner=True)], [person()], [person()]]
        args = SimpleNamespace(min_face=65, owner_threshold=.363, seconds=15.,
                               fps=5., idle_fps=1., lock=False, simulate_self=False,
                               no_preview=False, camera=0)
        with (patch.object(guard, 'load_owner', return_value=np.stack([person().feature])),
              patch.object(guard, 'MacLock', return_value=locker),
              patch.object(guard, 'Vision', return_value=detector),
              patch.object(guard, 'camera', return_value=cap),
              patch.object(guard, 'read_frame', return_value=None),
              patch.object(guard, 'show', side_effect=[0, 0, 0, ord('q')]),
              patch.object(guard.cv2, 'destroyAllWindows'),
              patch.object(guard.time, 'monotonic', side_effect=lambda: clock.now),
              patch.object(guard.time, 'time', side_effect=lambda: 1000 + clock.now),
              patch.object(guard.time, 'sleep', side_effect=sleep),
              contextlib.redirect_stdout(io.StringIO())):
            guard.watch(args)
        np.testing.assert_allclose(clock.sleeps, [1., 1., .2])
        locker.lock.assert_not_called()
        cap.release.assert_called_once()

    def test_invalid_frequency_order_is_rejected_before_opening_camera(self):
        p = subprocess.run([sys.executable, str(guard.ROOT / 'guard.py'), 'watch', '--idle-fps', '10', '--fps', '5'],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn('idle-fps 不能高于 fps', p.stderr)


class TimingTests(unittest.TestCase):
    def test_same_person_reaches_fifteen_seconds_once(self):
        c = WatchCounter()
        self.assertFalse(tick(c, 0, 14, person()))
        self.assertEqual(len(tick(c, 15, 20, person())), 1)

    def test_owner_never_triggers(self):
        self.assertFalse(tick(WatchCounter(), 0, 40, person(owner=True)))

    def test_looking_away_resets_count(self):
        c = WatchCounter()
        tick(c, 0, 10, person())
        c.update([person(state='away')], 11)
        self.assertFalse(tick(c, 12, 26, person()))
        self.assertEqual(len(tick(c, 27, 27, person())), 1)

    def test_missing_detection_breaks_continuity(self):
        c = WatchCounter()
        tick(c, 0, 10, person())
        c.update([], 10.2)
        tracks, events = c.update([person()], 10.4)
        self.assertEqual(tracks[0].seconds, 0)
        self.assertFalse(events)

    def test_different_people_cannot_pool_time(self):
        c = WatchCounter()
        tick(c, 0, 10, person(0))
        self.assertFalse(tick(c, 11, 20, person(1)))

    def test_simultaneous_people_have_separate_clocks(self):
        c = WatchCounter()
        tick(c, 0, 10, person(0, x=100))
        tick(c, 11, 14, person(0, x=100), person(1, x=400))
        tracks, events = c.update([person(0, x=100), person(1, x=400)], 15)
        self.assertEqual(len(events), 1)
        self.assertEqual(tracks[1].seconds, 4)

    def test_camera_stall_does_not_add_unseen_time(self):
        c = WatchCounter()
        tick(c, 0, 10, person())
        tracks, events = c.update([person()], 30)
        self.assertEqual(tracks[0].seconds, 0)
        self.assertFalse(events)

    def test_short_blink_pauses_without_adding_time(self):
        c = WatchCounter()
        tick(c, 0, 10, person())
        c.update([person(state='blink')], 10.2)
        tracks, _ = c.update([person()], 10.4)
        self.assertEqual(tracks[0].seconds, 10)
        tracks, _ = c.update([person()], 10.6)
        self.assertAlmostEqual(tracks[0].seconds, 10.2)

    def test_long_closed_eyes_reset(self):
        c = WatchCounter()
        tick(c, 0, 10, person())
        c.update([person(state='blink')], 10.2)
        c.update([person(state='blink')], 11.0)
        tracks, _ = c.update([person()], 11.2)
        self.assertEqual(tracks[0].seconds, 0)

    def test_owner_reclassification_clears_suspicion(self):
        c = WatchCounter()
        tick(c, 0, 10, person())
        c.update([person(owner=True)], 11)
        tracks, _ = c.update([person()], 12)
        self.assertEqual(tracks[0].seconds, 0)

    def test_unknown_gaze_breaks_continuity(self):
        c = WatchCounter()
        tick(c, 0, 10, person())
        c.update([person(state='unknown')], 11)
        tracks, _ = c.update([person()], 12)
        self.assertEqual(tracks[0].seconds, 0)

    def test_reordered_detections_keep_individual_ids(self):
        c = WatchCounter()
        first, _ = c.update([person(0, x=100), person(1, x=400)], 0)
        second, _ = c.update([person(1, x=400), person(0, x=100)], 1)
        self.assertEqual([t.number for t in second], [first[1].number, first[0].number])


class SafetyTests(unittest.TestCase):
    def test_preview_never_calls_lock(self):
        locker = Mock()
        track = guard.Track(1, (0, 0, 100, 100), unit([1]), 0, seconds=15)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(guard.handle_trigger(False, locker, track))
        locker.lock.assert_not_called()

    def test_armed_trigger_calls_lock(self):
        locker = Mock()
        track = guard.Track(1, (0, 0, 100, 100), unit([1]), 0, seconds=15)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(guard.handle_trigger(True, locker, track))
        locker.lock.assert_called_once_with()

    def test_owner_features_save_load_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'private' / 'owner.npz'
            with patch.object(guard, 'OWNER_FILE', target):
                features = [person().feature for _ in range(20)]
                guard.save_owner(features)
                np.testing.assert_allclose(guard.load_owner(), features)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_invalid_face_embedding_rejected(self):
        for value in ([0., 0.], [float('nan'), 1.], [float('inf'), 1.]):
            with self.assertRaises(ValueError):
                unit(value)

    def test_invalid_threshold_rejected(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with self.assertRaises(Exception):
                guard.positive(value)

    def test_duplicate_process_lock_is_rejected_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(guard, 'INSTANCE_FILE', Path(directory) / 'private' / 'running.lock'):
                first = guard.acquire_instance()
                try:
                    with self.assertRaises(RuntimeError):
                        guard.acquire_instance()
                finally:
                    first.close()
                guard.acquire_instance().close()

    def test_manual_lock_test_has_cancellable_countdown(self):
        locker = Mock()
        with (patch.object(guard, 'MacLock', return_value=locker),
              patch.object(guard.time, 'sleep') as sleep,
              contextlib.redirect_stdout(io.StringIO())):
            guard.test_lock(None)
        self.assertEqual(sleep.call_count, 5)
        locker.lock.assert_called_once_with()

    def test_single_person_simulation_preserves_profile_and_checks_gaze(self):
        detector = guard.Vision.__new__(guard.Vision)
        detector.owner_threshold = .363
        detector.faces = Mock(return_value=[np.zeros(15)])
        detector.feature = Mock(return_value=person().feature)
        detector.gaze = Mock(return_value=('look', 'simulated owner'))
        owners = np.stack([person().feature])
        original = owners.copy()
        actual = detector.observe(None, owners)
        self.assertTrue(actual[0].owner)
        detector.gaze.assert_not_called()
        simulated = detector.observe(None, owners, simulate_self=True)
        self.assertFalse(simulated[0].owner)
        self.assertEqual(simulated[0].state, 'look')
        self.assertAlmostEqual(simulated[0].owner_score, 1)
        detector.gaze.assert_called_once()
        np.testing.assert_array_equal(owners, original)

    def test_single_person_simulation_cannot_arm_lock(self):
        p = subprocess.run([sys.executable, str(guard.ROOT / 'guard.py'), 'watch', '--simulate-self', '--lock'],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn('单人模拟只允许预览', p.stderr)


if __name__ == '__main__':
    unittest.main()
