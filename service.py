#!/usr/bin/env python3
"""Install/manage a per-user launchd service; retain explicit human stop control."""
import argparse
from datetime import datetime
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

SOURCE = Path(__file__).resolve().parent
APP = Path.home() / 'Library/Application Support/ScreenPrivacyGuard'
STATE = APP / 'state'
CAPTURES = Path.home() / 'Pictures/ScreenPrivacyGuard'
LABEL = 'com.local.screen-privacy-guard'
PLIST = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
STATUS = STATE / 'status.json'
LOG = APP / 'logs/service.log'
BUNDLE = APP / 'ScreenPrivacyGuard.app'
HOST = BUNDLE / 'Contents/MacOS/ScreenPrivacyGuard'
DOMAIN = f'gui/{os.getuid()}'
TARGET = f'{DOMAIN}/{LABEL}'


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def ctl(*args):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, errors='replace')


def configuration():
    return {
        'Label': LABEL,
        'ProgramArguments': [str(HOST), '-u', str(APP/'service.py'), 'run'],
        'WorkingDirectory': str(APP), 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 10, 'ExitTimeOut': 10,
        'LimitLoadToSessionType': 'Aqua', 'ProcessType': 'Background',
        'AssociatedBundleIdentifiers': [LABEL+'.camera'],
        'EnvironmentVariables': {'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
        'StandardOutPath': str(APP/'logs/launchd.log'),
        'StandardErrorPath': str(APP/'logs/launchd.log'),
    }


def deploy(*, register_agent=True):
    for directory in (APP, STATE, APP/'logs', APP/'.private', CAPTURES):
        private_directory(directory)
    if not (SOURCE/'.private/owner.npz').exists():
        raise RuntimeError('请先录入你的人脸。')
    python = APP/'.venv/bin/python'
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(APP/'.venv')], check=True)
    requirements = (SOURCE/'requirements.txt').read_bytes()
    digest = hashlib.sha256(requirements).hexdigest()
    stamp = APP/'.requirements-hash'
    if not stamp.exists() or stamp.read_text().strip() != digest:
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(SOURCE/'requirements.txt')], check=True)
        stamp.write_text(digest)
    for name in ('guard.py', 'setup_models.py', 'service.py', 'requirements.txt', 'CameraHost.swift'):
        if SOURCE != APP:
            shutil.copy2(SOURCE/name, APP/name)
    private_directory(APP/'models')
    for source in (SOURCE/'models').iterdir():
        if source.is_file() and SOURCE != APP:
            destination = APP/'models'/source.name
            if not destination.exists() or destination.stat().st_size != source.stat().st_size:
                shutil.copy2(source, destination)
    if SOURCE != APP:
        shutil.copy2(SOURCE/'.private/owner.npz', APP/'.private/owner.npz')
    (APP/'.private/owner.npz').chmod(0o600)
    private_directory(HOST.parent)
    info_bytes = plistlib.dumps({
        'CFBundleIdentifier': LABEL+'.camera', 'CFBundleName': 'Screen Privacy Guard',
        'CFBundleDisplayName': '屏幕旁观保护', 'CFBundleExecutable': 'ScreenPrivacyGuard',
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'LSUIElement': True,
        'NSCameraUsageDescription': '在本机识别旁观者。非主人疑似注视超过 5 秒时保存脸部截图，达到 15 秒时锁屏。',
    })
    host_digest = hashlib.sha256((APP/'CameraHost.swift').read_bytes() + info_bytes).hexdigest()
    host_stamp = APP/'.camera-host-hash'
    if not HOST.exists() or not host_stamp.exists() or host_stamp.read_text() != host_digest:
        (BUNDLE/'Contents/Info.plist').write_bytes(info_bytes)
        subprocess.run(['/usr/bin/xcrun', 'swiftc', '-O', str(APP/'CameraHost.swift'), '-o', str(HOST)], check=True)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(BUNDLE)], check=True)
        host_stamp.write_text(host_digest)
    authorization_entry = SOURCE/'8-摄像头授权.app'
    if not authorization_entry.exists() and not authorization_entry.is_symlink():
        authorization_entry.symlink_to(BUNDLE, target_is_directory=True)
    if register_agent:
        PLIST.parent.mkdir(exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(configuration()))
        PLIST.chmod(0o600)


def start():
    # Update only this service, never kill unrelated foreground camera programs.
    if ctl('print', TARGET).returncode == 0:
        result = ctl('bootout', TARGET)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
    deploy()
    ctl('enable', TARGET)
    result = ctl('bootstrap', DOMAIN, str(PLIST))
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    print('后台常驻服务已加载；登录后自动启动，退出后由系统重新拉起。')
    print('截图目录：', CAPTURES)
    print('停止：运行“5-停止后台常驻.command”。')


def owned_processes():
    """Find only our runtime's Python workers and native camera host."""
    output = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True, errors='replace')
    found = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, command = int(parts[0]), parts[1]
        if pid == os.getpid():
            continue
        python_job = any(str(APP/name) + suffix in command
                         for name, suffix in (('guard.py', ' watch'), ('service.py', ' run')))
        native_job = command == str(HOST) or command.startswith(str(HOST)+' ')
        if not (python_job or native_job):
            continue
        check = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True,
                               text=True, errors='replace')
        executable = Path(check.stdout.strip()).name.lower()
        if check.returncode == 0 and ((python_job and executable.startswith('python'))
                                     or (native_job and executable == 'screenprivacyguard')):
            found[pid] = command
    return found


def stop():
    result = ctl('disable', TARGET)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    if ctl('print', TARGET).returncode == 0:
        result = ctl('bootout', TARGET)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
    # The native host's grandchildren can survive bootout in another process group.
    for pid in owned_processes():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while owned_processes() and time.monotonic() < deadline:
        time.sleep(.1)
    for pid in owned_processes():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    previous = read_status()
    previous.update(state='stopped', pid=None, updated_at=time.time(), stopped_by_user=True)
    private_directory(STATE)
    with tempfile.NamedTemporaryFile(mode='w', dir=STATE, delete=False) as handle:
        json.dump(previous, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(STATUS)
    print('已人为停止后台服务，并关闭登录自启；截图和人脸资料保留。再次运行启动入口可恢复。')


def read_status():
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


def is_disabled(output):
    return bool(re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*(?:true|disabled)\b', output))


def describe(as_json=False):
    result = ctl('print', TARGET)
    status = read_status()
    match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M)
    disabled = ctl('print-disabled', DOMAIN)
    info = {
        'loaded': result.returncode == 0,
        'supervisor_pid': int(match.group(1)) if match else None,
        'worker_pid': status.get('pid'), 'worker_state': status.get('state'),
        'heartbeat_age_seconds': round(time.time() - status['updated_at'], 1) if 'updated_at' in status else None,
        'autostart_config_exists': PLIST.exists(),
        'disabled': is_disabled(disabled.stdout),
        'snapshot_directory': str(CAPTURES), 'log_file': str(LOG), 'worker_details': status,
    }
    if as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print('后台服务：', '已加载' if info['loaded'] else '未加载')
        print('登录自启：', '关闭' if info['disabled'] or not PLIST.exists() else '开启')
        names = {'monitoring': '正在监测', 'locked': '屏幕已锁定，暂停摄像头',
                 'camera_retry': '摄像头中断，正在自动重连',
                 'camera_permission_required': '等待你允许摄像头权限',
                 'camera_permission_denied': '摄像头权限被关闭，请在系统设置中允许',
                 'camera_permission_granted': '摄像头已授权，等待后台启动', 'stopped': '已停止'}
        print('当前状态：', names.get(status.get('state'), status.get('state', '尚无状态记录')))
        print('状态更新距今：', info['heartbeat_age_seconds'], '秒')
        print('截图目录：', CAPTURES)
        print('运行日志：', LOG)
    return info


def run():
    os.umask(0o077)
    private_directory(APP/'logs')
    handler = RotatingFileHandler(LOG, maxBytes=2*1024*1024, backupCount=3, encoding='utf-8')
    os.chmod(LOG, 0o600)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger = logging.getLogger('screen-privacy-guard')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    stop_event = threading.Event()
    child = None
    def stop_requested(*_):
        stop_event.set()
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    args = [str(APP/'.venv/bin/python'), '-u', str(APP/'guard.py'), 'watch', '--lock',
            '--no-preview', '--save-snapshots', '--snapshot-seconds', '5',
            '--snapshot-dir', str(CAPTURES), '--status-file', str(STATUS),
            '--seconds', '15', '--idle-fps', '1', '--fps', '5']
    environment = dict(os.environ, PYTHONUNBUFFERED='1', MPLCONFIGDIR=str(APP/'matplotlib-cache'))
    started = last_wall = time.time()
    logger.info('监督进程启动 pid=%s；启动摄像头工作进程。', os.getpid())
    child = subprocess.Popen(args, cwd=APP, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors='replace', bufsize=1, env=environment,
                             start_new_session=True)
    logger.info('工作进程 pid=%s', child.pid)
    def forward_output():
        for line in child.stdout:
            logger.info('%s', line.rstrip())
    reader = threading.Thread(target=forward_output, daemon=True)
    reader.start()
    watchdog_failed = False
    while child.poll() is None:
        if stop_event.is_set():
            try:
                child.wait(timeout=6)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
            break
        wall = time.time()
        if wall - last_wall > 10 or wall < last_wall:
            started = wall  # Allow the worker to recover after system sleep.
        last_wall = wall
        status = read_status()
        last_update = status.get('updated_at', started)
        if wall - max(last_update, started) > 90:
            logger.error('工作进程心跳超过 90 秒未更新，终止卡住的进程，交给 launchd 重启。')
            watchdog_failed = True
            os.killpg(child.pid, signal.SIGKILL)
            break
        time.sleep(1)
    code = child.wait()
    reader.join(timeout=3)
    if reader.is_alive():
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        reader.join(timeout=1)
    logger.info('工作进程退出 code=%s；human_stop=%s', code, stop_event.is_set())
    logger.removeHandler(handler)
    handler.close()
    return 0 if stop_event.is_set() else (1 if watchdog_failed or code != 0 else 0)


def main():
    parser = argparse.ArgumentParser(description='防旁观后台常驻服务')
    parser.add_argument('command', choices=['start', 'stop', 'status', 'run'])
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if args.command != 'run':
        {'start': start, 'stop': stop, 'status': lambda: describe(args.json)}[args.command]()
        return 0
    return run()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        sys.exit(1)
