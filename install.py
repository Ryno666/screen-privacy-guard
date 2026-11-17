#!/usr/bin/env python3
"""Prepare the background runtime and UI without starting monitoring."""
import argparse
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def install():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('当前版本面向 Apple Silicon Mac。')
    result = subprocess.run(['/usr/bin/xcrun', '--find', 'swiftc'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('需要 Xcode Command Line Tools。请先运行 xcode-select --install。')
    for line in subprocess.check_output(['ps', '-axo', 'command='], text=True, errors='replace').splitlines():
        if line.strip() == str(Path.home()/'Applications/屏幕旁观保护.app/Contents/MacOS/ScreenPrivacyControl'):
            raise RuntimeError('请先从菜单退出“屏幕旁观保护”控制界面，再安装更新。关闭窗口只会隐藏界面。')
    subprocess.run(['/bin/bash', str(ROOT/'setup.sh')], cwd=ROOT, check=True)
    if not (ROOT/'.private/owner.npz').exists():
        raise RuntimeError('尚未录入白名单人脸。请先运行“1-录入我的人脸.command”，完成后再安装。')
    python = str(ROOT/'.venv/bin/python')
    subprocess.run([python, '-c', 'import service; service.deploy(register_agent=False)'], cwd=ROOT, check=True)
    subprocess.run([python, str(ROOT/'build_control_ui.py')], cwd=ROOT, check=True)
    print('安装完成。没有启动监测，也没有开启登录自启。')
    print('打开 ~/Applications/屏幕旁观保护.app，点击“启动监测”即可开启。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        install()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f'安装未完成：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
