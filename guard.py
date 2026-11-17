#!/usr/bin/env python3
"""Local, approximate shoulder-surf detection for macOS. Default: preview only."""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
OWNER_FILE = ROOT / '.private' / 'owner.npz'
INSTANCE_FILE = Path.home() / 'Library/Application Support/ScreenPrivacyGuard/state/running.lock'
DEFAULT_CAPTURE_DIR = Path.home() / 'Pictures/ScreenPrivacyGuard'
WINDOW = 'Screen Privacy Guard | Q / Esc = stop'


def unit(vector):
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(vector)
    if not np.isfinite(vector).all() or norm < 1e-6:
        raise ValueError('无效的人脸特征。')
    return vector / norm


@dataclass
class Observation:
    box: tuple[float, float, float, float]
    feature: np.ndarray
    state: str  # look, away, blink, unknown
    owner_score: float = -1.0
    owner: bool = False
    detail: str = ''


@dataclass
class Track:
    number: int
    box: tuple[float, float, float, float]
    feature: np.ndarray
    last: float
    state: str = 'unknown'
    owner: bool = False
    seconds: float = 0.0
    blink_since: float | None = None
    fired: bool = False
    snapshot_saved: bool = False
    run_max_owner_score: float = -1.0


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class SnapshotRecorder:
    """One local face crop per uninterrupted, confidently non-owner gaze event."""
    def __init__(self, directory, seconds=5.0, owner_exclusion=.30):
        self.directory, self.seconds = Path(directory).expanduser(), seconds
        self.owner_exclusion = owner_exclusion

    def maybe_save(self, frame, observation, track):
        if (observation.owner or track.owner or observation.state != 'look'
                or track.seconds <= self.seconds or track.snapshot_saved
                or max(track.run_max_owner_score, observation.owner_score) >= self.owner_exclusion):
            return None
        ih, iw = frame.shape[:2]
        x, y, w, h = observation.box
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(iw, math.ceil(x+w)), min(ih, math.ceil(y+h))
        if x1 <= x0 or y1 <= y0:
            return None
        ok, encoded = cv2.imencode('.jpg', frame[y0:y1, x0:x1], [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise RuntimeError('截图编码失败。')
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        directory = self.directory / now.strftime('%Y-%m-%d')
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        name = now.strftime('%H%M%S_%f') + f'_track-{track.number}_{uuid.uuid4().hex[:6]}'
        target = directory / f'{name}.jpg'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded.tobytes())
        # The photo is already durable: metadata errors must not cause duplicate photos.
        track.snapshot_saved = True
        write_json(target.with_suffix('.json'), {
            'time': now.isoformat(), 'gaze_seconds': round(track.seconds, 3),
            'track_id': track.number, 'owner_similarity': observation.owner_score,
            'max_owner_similarity_during_event': track.run_max_owner_score,
            'classification_is_estimate': True, 'kind': 'suspected_non_owner_gaze',
            'crop': [x0, y0, x1-x0, y1-y0],
        })
        return target


class AdaptiveRate:
    """Scan slowly until another person appears; never changes gaze timers."""
    def __init__(self, idle_fps=1.0, active_fps=5.0, hold_seconds=2.0):
        if not (0 < idle_fps <= active_fps <= 30) or hold_seconds < 0:
            raise ValueError('检查频率需满足 0 < 空闲频率 <= 活跃频率 <= 30。')
        self.idle_fps, self.active_fps = idle_fps, active_fps
        self.hold_seconds = hold_seconds
        self.last_other: float | None = None

    def reset(self):
        self.last_other = None

    def update(self, observations: list[Observation], now: float):
        if self.last_other is not None and now < self.last_other:
            self.reset()
        if any(not o.owner for o in observations):
            self.last_other = now
            return self.active_fps
        if self.last_other is not None and now - self.last_other < self.hold_seconds:
            return self.active_fps
        return self.idle_fps


class WatchCounter:
    """Each identity has its own clock; missing/away/stalled frames break it.

    A blink of <= 0.7 s pauses the clock. Blink time is never added.
    Appearance and position must both agree before reusing a clock.
    """
    def __init__(self, threshold=15.0, max_gap=1.2, blink_grace=0.7):
        self.threshold, self.max_gap, self.blink_grace = threshold, max_gap, blink_grace
        self.tracks: list[Track] = []
        self.next_id = 1

    def reset(self):
        self.tracks.clear()

    def update(self, observations: list[Observation], now: float):
        self.tracks = [t for t in self.tracks if 0 <= now - t.last <= self.max_gap]
        pairs = []
        for oi, obs in enumerate(observations):
            for ti, track in enumerate(self.tracks):
                similarity = float(obs.feature @ track.feature)
                x, y, w, h = obs.box
                a, b, c, d = track.box
                distance = math.hypot(x + w/2 - a - c/2, y + h/2 - b - d/2)
                distance /= max(w, h, c, d, 1)
                if similarity >= 0.30 and distance <= 1.25:
                    pairs.append((similarity - 0.15 * distance, oi, ti))
        assigned, used = {}, set()
        for _, oi, ti in sorted(pairs, reverse=True):
            if oi not in assigned and ti not in used:
                assigned[oi] = self.tracks[ti]
                used.add(ti)
        # Even a missed detection breaks continuity; it never adds unseen time.
        for ti, track in enumerate(self.tracks):
            if ti not in used:
                track.seconds, track.state, track.fired = 0.0, 'unknown', False
                track.blink_since = None
                track.snapshot_saved, track.run_max_owner_score = False, -1.0
        visible, triggers = [], []
        for oi, obs in enumerate(observations):
            track = assigned.get(oi)
            if track is None:
                track = Track(self.next_id, obs.box, obs.feature.copy(), now)
                self.next_id += 1
                self.tracks.append(track)
            delta = now - track.last
            if obs.owner or obs.state in ('away', 'unknown'):
                track.seconds, track.blink_since, track.fired = 0.0, None, False
            elif obs.state == 'look':
                if not track.owner and track.state == 'look' and 0 <= delta <= self.max_gap:
                    track.seconds += delta
                elif (not track.owner and track.state == 'blink'
                      and track.blink_since is not None
                      and now - track.blink_since <= self.blink_grace):
                    pass  # Resume without adding blink time.
                else:
                    track.seconds, track.fired = 0.0, False
                track.blink_since = None
            elif obs.state == 'blink':
                if track.state == 'look' and not track.owner:
                    track.blink_since = now
                if (track.blink_since is None
                        or now - track.blink_since > self.blink_grace):
                    track.seconds, track.fired = 0.0, False
                    track.blink_since = None
            else:
                raise ValueError(f'Unknown state: {obs.state}')
            track.box, track.last, track.state, track.owner = obs.box, now, obs.state, obs.owner
            if track.seconds == 0:
                track.snapshot_saved, track.run_max_owner_score = False, obs.owner_score
            else:
                track.run_max_owner_score = max(track.run_max_owner_score, obs.owner_score)
            track.feature = unit(0.8 * track.feature + 0.2 * obs.feature)
            if not track.owner and track.seconds >= self.threshold and not track.fired:
                track.fired = True
                triggers.append(track)
            visible.append(track)
        return visible, triggers


class MacLock:
    """No Accessibility permission or password required. No lock in __init__."""
    def __init__(self):
        if sys.platform != 'darwin':
            raise RuntimeError('此版本只支持 macOS。')
        self.login = ctypes.CDLL('/System/Library/PrivateFrameworks/login.framework/Versions/Current/login')
        self.action = self.login.SACLockScreenImmediate
        self.action.argtypes, self.action.restype = [], None
        self.cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.cg.CGSessionCopyCurrentDictionary.argtypes = []
        self.cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        self.cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        self.cf.CFBooleanGetValue.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes, self.cf.CFRelease.restype = [ctypes.c_void_p], None

    def locked(self):
        session = self.cg.CGSessionCopyCurrentDictionary()
        if not session:
            raise RuntimeError('无法读取当前 macOS 登录状态，请在本机桌面运行。')
        key = self.cf.CFStringCreateWithCString(None, b'CGSSessionScreenIsLocked', 0x08000100)
        try:
            value = self.cf.CFDictionaryGetValue(session, key)
            return bool(value and self.cf.CFBooleanGetValue(value))
        finally:
            self.cf.CFRelease(key)
            self.cf.CFRelease(session)

    def lock(self):
        self.action()
        for _ in range(30):
            if self.locked():
                return
            time.sleep(0.1)
        raise RuntimeError('已发出锁屏请求，但未确认屏幕已锁定；程序已停止。')


class Vision:
    def __init__(self, min_face=65, owner_threshold=0.363):
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision
        from setup_models import ensure_models
        ensure_models(download=False)
        self.mp = mp
        self.min_face, self.owner_threshold = min_face, owner_threshold
        self.detector = cv2.FaceDetectorYN.create(
            str(ROOT / 'models/face_detection_yunet_2023mar.onnx'), '',
            (640, 480), 0.80, 0.30, 1000)
        self.recognizer = cv2.FaceRecognizerSF.create(
            str(ROOT / 'models/face_recognition_sface_2021dec.onnx'), '')
        options = vision.FaceLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=str(ROOT / 'models/face_landmarker.task')),
            running_mode=vision.RunningMode.IMAGE, num_faces=1,
            output_facial_transformation_matrixes=True,
            min_face_detection_confidence=0.5, min_face_presence_confidence=0.5)
        self.landmarker = vision.FaceLandmarker.create_from_options(options)

    def close(self):
        self.landmarker.close()

    def faces(self, frame):
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(frame)
        if faces is None:
            return []
        return [f for f in faces if min(f[2], f[3]) >= self.min_face]

    def feature(self, frame, face):
        aligned = self.recognizer.alignCrop(frame, face)
        return unit(self.recognizer.feature(aligned))

    def gaze(self, frame, face):
        # Crop around each separately so a bystander also gets full-resolution eyes.
        x, y, w, h = map(float, face[:4])
        ih, iw = frame.shape[:2]
        x0, y0 = max(0, int(x - w*.3)), max(0, int(y - h*.3))
        x1, y1 = min(iw, int(x + w*1.3)), min(ih, int(y + h*1.3))
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return 'unknown', 'empty crop'
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        result = self.landmarker.detect(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb))
        if not result.face_landmarks or not result.facial_transformation_matrixes:
            return 'unknown', 'eyes unavailable'
        mesh = result.face_landmarks[0]
        points = np.array([(p.x * (x1-x0) + x0, p.y * (y1-y0) + y0) for p in mesh])
        center = (points.min(axis=0) + points.max(axis=0)) / 2
        if abs(center[0] - x - w/2) > w*.55 or abs(center[1] - y - h/2) > h*.55:
            return 'unknown', 'face association uncertain'
        r = np.asarray(result.facial_transformation_matrixes[0])[:3, :3]
        yaw = math.degrees(math.atan2(-r[2, 0], math.hypot(r[0, 0], r[1, 0])))
        pitch = math.degrees(math.atan2(r[2, 1], r[2, 2]))
        roll = math.degrees(math.atan2(r[1, 0], r[0, 0]))
        detail = f'yaw={yaw:.0f} pitch={pitch:.0f}'
        if abs(yaw) > 25 or abs(pitch) > 25 or abs(roll) > 35:
            return 'away', detail
        # Iris centers 468/473; eye corners and lids from the 478-point mesh.
        if len(points) < 478:
            return 'unknown', 'iris unavailable'
        ratios = []
        for a, b, top, bottom, iris in ((33, 133, 159, 145, 468), (362, 263, 386, 374, 473)):
            left, right = sorted((points[a], points[b]), key=lambda p: p[0])
            axis = right - left
            width = float(np.linalg.norm(axis))
            opening = float(np.linalg.norm(points[top] - points[bottom]))
            if width < 8:
                return 'unknown', 'eyes too small'
            if opening / width < .12:
                return 'blink', detail
            horizontal = float((points[iris] - left) @ axis / (width*width))
            vertical_axis = points[bottom] - points[top]
            vertical = float((points[iris] - points[top]) @ vertical_axis / (opening*opening))
            ratios.append((horizontal, vertical))
        if all(.28 <= a <= .72 and .10 <= b <= .90 for a, b in ratios):
            return 'look', detail
        return 'away', detail + ' eyes averted'

    def observe(self, frame, owners, simulate_self=False):
        observations = []
        for face in self.faces(frame):
            feature = self.feature(frame, face)
            score = float(np.max(owners @ feature)) if len(owners) else -1.0
            owner = score >= self.owner_threshold and not simulate_self
            state, detail = ('look', '') if owner else self.gaze(frame, face)
            observations.append(Observation(tuple(map(float, face[:4])), feature, state, score, owner, detail))
        return observations


def camera(index):
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError('无法打开摄像头。请在“系统设置 → 隐私与安全性 → 摄像头”允许终端使用；也可换 --camera 编号。')
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 960)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, 15)
    return cap


def read_frame(cap):
    ok, frame = cap.read()
    if not ok or frame is None:
        raise RuntimeError('摄像头画面中断，监测已停止。检查权限或摄像头是否被其他应用占用。')
    return frame


def show(frame, observations=(), tracks=(), title='PREVIEW - will NOT lock'):
    canvas = frame.copy()
    for obs, track in zip(observations, tracks):
        x, y, w, h = map(int, obs.box)
        color = (60, 200, 60) if obs.owner else (30, 90, 240)
        label = 'YOU' if obs.owner else f'Other #{track.number} {obs.state} {track.seconds:.1f}s'
        label += f' match={obs.owner_score:.2f}'
        cv2.rectangle(canvas, (x, y), (x+w, y+h), color, 2)
        cv2.putText(canvas, label, (max(0, x), max(50, y-12)), cv2.FONT_HERSHEY_SIMPLEX, .48, color, 1)
        if obs.detail:
            cv2.putText(canvas, obs.detail, (max(0, x), min(canvas.shape[0]-10, y+h+18)), cv2.FONT_HERSHEY_SIMPLEX, .42, color, 1)
    cv2.putText(canvas, title, (15, 28), cv2.FONT_HERSHEY_SIMPLEX, .65, (0, 220, 255), 2)
    cv2.imshow(WINDOW, canvas)
    key = cv2.waitKey(1) & 0xff
    if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
        return ord('q')
    return key


def save_owner(features):
    OWNER_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    OWNER_FILE.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(dir=OWNER_FILE.parent, suffix='.npz')
    try:
        with os.fdopen(fd, 'wb') as handle:
            np.savez_compressed(handle, embeddings=np.asarray(features, dtype=np.float32))
        os.chmod(name, 0o600)
        os.replace(name, OWNER_FILE)
    finally:
        Path(name).unlink(missing_ok=True)


def load_owner():
    if not OWNER_FILE.exists():
        raise RuntimeError('还没有录入你的人脸。请先双击“1-录入我的人脸.command”。')
    with np.load(OWNER_FILE, allow_pickle=False) as data:
        features = data['embeddings']
    if features.ndim != 2 or features.shape[1] != 128 or len(features) < 5:
        raise RuntimeError('人脸资料无效，请重新录入。')
    return np.stack([unit(f) for f in features])


def enroll(args):
    print('录入时请确保画面里只有你。看向屏幕，可轻微转头；不保存照片。', flush=True)
    print('预览中按空格开始，Q / Esc 取消。已有资料只会在成功录入后替换。', flush=True)
    detector, cap = Vision(min_face=120), None
    features, started, last_sample = [], None, 0.0
    try:
        cap = camera(args.camera)
        print('摄像头已打开。请点击摄像头窗口，再按空格开始录入。', flush=True)
        while True:
            frame = read_frame(cap)
            faces = detector.faces(frame)
            now = time.monotonic()
            title = 'Only YOU in view. SPACE = enroll | Q = cancel'
            if started is not None:
                remaining = 3 - (now - started)
                title = f'Starting in {max(0, math.ceil(remaining))}s'
                if remaining <= 0:
                    title = f'Look at screen; gently turn head. Samples {len(features)}/20'
                    if len(faces) == 1 and now - last_sample >= .35:
                        f = detector.feature(frame, faces[0])
                        # Reject accidental identity switches during enrollment.
                        if features and float(np.max(np.stack(features) @ f)) < .30:
                            raise RuntimeError('录入过程中人脸变化过大，未覆盖旧资料。请重试。')
                        features.append(f)
                        last_sample = now
                    elif len(faces) != 1:
                        title = 'Need exactly ONE clear face; move closer'
            key = show(frame, title=title)
            if key in (ord('q'), 27):
                print('已取消录入。')
                return
            if key == 32 and started is None:
                started = now
            if len(features) >= 20:
                save_owner(features)
                print('已录入 20 份本地人脸特征。下一步：运行预览，确认你显示为 YOU。', flush=True)
                return
    finally:
        if cap is not None:
            cap.release()
        detector.close()
        cv2.destroyAllWindows()


def handle_trigger(armed, locker, track):
    if armed:
        print(f'其他人 #{track.number} 疑似持续看向屏幕 {track.seconds:.1f} 秒，正在锁屏。', flush=True)
        locker.lock()
        return True
    print(f'【预览】其他人 #{track.number} 已达到锁屏条件（{track.seconds:.1f} 秒）；未执行锁屏。', flush=True)
    return False


def watch(args):
    owners = load_owner()
    locker = MacLock()
    locker.locked()  # Read-only preflight, never a lock.
    detector = Vision(args.min_face, args.owner_threshold)
    counter = WatchCounter(args.seconds)
    rate = AdaptiveRate(args.idle_fps, args.fps)
    cap = None
    ready_at, last_wall, last_report = time.monotonic() + 5, time.time(), 0.0
    frames = owner_frames = trigger_count = 0
    snapshot_count, last_status = 0, 0.0
    recorder = (SnapshotRecorder(getattr(args, 'snapshot_dir', DEFAULT_CAPTURE_DIR),
                                getattr(args, 'snapshot_seconds', 5.0), min(.30, args.owner_threshold))
                if getattr(args, 'save_snapshots', False) else None)
    def status(state, force=False, **details):
        nonlocal last_status
        path = getattr(args, 'status_file', None)
        if not path:
            return
        wall = time.time()
        if not force and wall - last_status < 5:
            return
        write_json(path, {'state': state, 'pid': os.getpid(), 'updated_at': wall,
                         'frames': frames, 'owner_frames': owner_frames,
                         'snapshot_count': snapshot_count, **details})
        last_status = wall
    longest = 0.0
    print(f'{"正式监测，允许锁屏" if args.lock else "预览模式，不会锁屏"}；阈值 {args.seconds:g} 秒。Q / Esc 或 Ctrl+C 停止。', flush=True)
    print(f'自适应检查：只有你或无人时 {args.idle_fps:g} 次/秒；出现其他人时 {args.fps:g} 次/秒，离开 2 秒后降频。', flush=True)
    if args.simulate_self:
        print('【单人模拟】临时把画面中的你当作旁观者，只测试朝向和计时；不会锁屏、不修改录入资料。', flush=True)
    if recorder:
        print(f'非主人疑似连续注视超过 {recorder.seconds:g} 秒时保存一张脸部截图：{recorder.directory}', flush=True)
    try:
        while True:
            loop_start = time.monotonic()
            if locker.locked():
                counter.reset()
                rate.reset()
                status('locked')
                if cap is not None:
                    cap.release()
                    cap = None
                if not args.no_preview:
                    cv2.destroyAllWindows()
                time.sleep(.5)
                ready_at = time.monotonic() + 10  # Give time to stop or move after unlock.
                continue
            try:
                if cap is None:
                    cap = camera(args.camera)
                frame = read_frame(cap)
            except (RuntimeError, cv2.error) as exc:
                print(f'摄像头暂不可用：{exc}；5 秒后自动重连。', flush=True)
                if cap is not None:
                    cap.release()
                    cap = None
                counter.reset()
                rate.reset()
                ready_at = time.monotonic() + 5
                status('camera_retry', force=True, last_error=str(exc))
                time.sleep(5)
                continue
            observations = detector.observe(frame, owners, simulate_self=args.simulate_self)
            frames += 1
            owner_frames += int(any(o.owner for o in observations))
            now, wall = time.monotonic(), time.time()
            check_fps = rate.update(observations, now)
            if wall - last_wall > counter.max_gap or wall < last_wall:
                counter.reset()  # Resume from sleep, inference stall, or clock change.
            last_wall = wall
            if now < ready_at:
                counter.reset()
                tracks = [Track(0, o.box, o.feature, now) for o in observations]
                triggers = []
            else:
                tracks, triggers = counter.update(observations, now)
            longest = max(longest, max((t.seconds for t in tracks), default=0.0))
            trigger_count += len(triggers)
            if recorder:
                for obs, track in zip(observations, tracks):
                    try:
                        saved = recorder.maybe_save(frame, obs, track)
                        if saved:
                            snapshot_count += 1
                            print(f'已保存非主人疑似注视事件截图：{saved}', flush=True)
                    except (OSError, RuntimeError, ValueError, cv2.error) as exc:
                        print(f'保存截图失败，监测继续：{exc}', flush=True)
            status('monitoring', seen_faces=len(observations), owner_faces=sum(o.owner for o in observations),
                   check_fps=check_fps, longest_gaze=round(longest, 2))
            if not args.no_preview:
                title = 'ARMED - automatic lock' if args.lock else 'PREVIEW - will NOT lock'
                if args.simulate_self:
                    title = 'SIMULATION: YOU = Other | will NOT lock'
                title += f' | checks {check_fps:g}/s'
                if now < ready_at:
                    title += f' | warmup {math.ceil(ready_at-now)}s'
                if show(frame, observations, tracks, title) in (ord('q'), 27):
                    return
            if now - last_report >= (2 if args.simulate_self else 10):
                details = '; '.join(f'#{t.number} {"YOU" if o.owner else o.state} {t.seconds:.1f}s {o.detail}' for o, t in zip(observations, tracks))
                print(f'监测中：看到 {len(observations)} 人，其中识别为你 {sum(o.owner for o in observations)} 人；目标检查频率 {check_fps:g} 次/秒。{details}', flush=True)
                last_report = now
            for track in triggers:
                if handle_trigger(args.lock, locker, track):
                    counter.reset()
                    break
            time.sleep(max(0, 1 / check_fps - (time.monotonic() - loop_start)))
    finally:
        if cap is not None:
            cap.release()
        detector.close()
        cv2.destroyAllWindows()
        status('stopped', force=True)
        print(f'本次统计：处理 {frames} 帧，其中 {owner_frames} 帧认出你；最高计时 {longest:.1f} 秒，达到条件 {trigger_count} 次。', flush=True)


def doctor(args):
    import mediapipe
    print(f'Python {sys.version.split()[0]}; OpenCV {cv2.__version__}; MediaPipe {mediapipe.__version__}')
    locker = MacLock()
    print(f'锁屏接口可加载，当前 locked={locker.locked()}；未执行锁屏。')
    vision = Vision()
    try:
        blank = np.zeros((480, 640, 3), np.uint8)
        assert not vision.faces(blank)
        assert vision.recognizer.feature(np.zeros((112, 112, 3), np.uint8)).size == 128
        vision.landmarker.detect(vision.mp.Image(image_format=vision.mp.ImageFormat.SRGB, data=blank))
        print('三个模型均完成实际推理；未打开摄像头。')
    finally:
        vision.close()
    print('你的人脸资料：' + ('已录入' if OWNER_FILE.exists() else '尚未录入'))


def test_lock(args):
    locker = MacLock()
    print('此命令将真实锁屏。5 秒内可按 Ctrl+C 取消。', flush=True)
    for remaining in range(5, 0, -1):
        print(f'{remaining}…', flush=True)
        time.sleep(1)
    locker.lock()
    print('系统已确认锁屏。', flush=True)


def acquire_instance():
    INSTANCE_FILE.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    INSTANCE_FILE.parent.chmod(0o700)
    handle = INSTANCE_FILE.open('a')
    INSTANCE_FILE.chmod(0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('已有一个监测或录入窗口在运行，请先在那个窗口按 Q 或 Ctrl+C 停止。') from None
    return handle


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('必须是大于 0 的有限数值')
    return result


def main():
    parser = argparse.ArgumentParser(description='本机防旁观锁屏：先 enroll，再 watch 预览，最后 watch --lock。')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('enroll', help='手动录入你的人脸，仅保存本机特征')
    p.add_argument('--camera', type=int, default=0)
    p = sub.add_parser('watch', help='监测；默认只预览，--lock 才真正锁屏')
    p.add_argument('--camera', type=int, default=0)
    p.add_argument('--lock', action='store_true', help='允许达到阈值时自动锁屏')
    p.add_argument('--seconds', type=positive, default=15.0)
    p.add_argument('--fps', '--active-fps', type=positive, default=5.0,
                   help='发现其他人后的目标检查频率，默认每秒 5 次')
    p.add_argument('--idle-fps', type=positive, default=1.0,
                   help='只有你或无人时的目标检查频率，默认每秒 1 次')
    p.add_argument('--min-face', type=positive, default=65)
    p.add_argument('--owner-threshold', type=positive, default=.363)
    p.add_argument('--no-preview', action='store_true', help='不显示画面；Ctrl+C 停止')
    p.add_argument('--simulate-self', action='store_true', help='单人模拟旁观者，仅预览，不修改主人资料，不可与 --lock 同用')
    p.add_argument('--save-snapshots', action='store_true', help='非主人持续注视超过阈值时保存本地脸部截图')
    p.add_argument('--snapshot-seconds', type=positive, default=5.)
    p.add_argument('--snapshot-dir', type=Path, default=DEFAULT_CAPTURE_DIR)
    p.add_argument('--status-file', type=Path, help='运行状态记录文件')
    sub.add_parser('doctor', help='离线检查依赖、模型和锁屏接口，不开摄像头、不锁屏')
    sub.add_parser('test-lock', help='倒计时 5 秒后真实锁屏，用于手动确认锁屏功能')
    args = parser.parse_args()
    if getattr(args, 'simulate_self', False) and args.lock:
        parser.error('单人模拟只允许预览，不能与 --lock 同时使用。')
    if getattr(args, 'simulate_self', False) and args.save_snapshots:
        parser.error('单人模拟不保存照片，不能与 --save-snapshots 同时使用。')
    if getattr(args, 'fps', 5) > 30 or getattr(args, 'owner_threshold', .363) > 1:
        parser.error('fps 不能超过 30；owner-threshold 不能超过 1。')
    if getattr(args, 'idle_fps', 1) > getattr(args, 'fps', 5):
        parser.error('idle-fps 不能高于 fps。')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    instance = None
    try:
        if args.command in ('enroll', 'watch', 'test-lock'):
            instance = acquire_instance()
        {'enroll': enroll, 'watch': watch, 'doctor': doctor, 'test-lock': test_lock}[args.command](args)
    except KeyboardInterrupt:
        print('\n已停止。')
    except (RuntimeError, ValueError, OSError, AttributeError, cv2.error) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1
    finally:
        if instance is not None:
            instance.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
