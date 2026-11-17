#!/usr/bin/env python3
"""Check the Git-tracked source, not just ignore rules, before publishing."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
blocked_parts = {'.private', '.venv', '__pycache__', 'models', 'captures', 'screenshots', 'dist'}
blocked_extensions = {'.npz', '.npy', '.jpg', '.jpeg', '.png', '.mp4', '.onnx', '.task', '.log', '.pem', '.key'}


def main():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    problems = []
    for name in filter(None, tracked):
        path = Path(name)
        if (blocked_parts.intersection(path.parts) or path.suffix.lower() in blocked_extensions
                or any(p.endswith('.app') for p in path.parts) or path.name.startswith('.env')
                or path.name.startswith(('LIVE_TEST_', 'PERSISTENCE_SETUP_', 'CONTROL_UI_'))):
            problems.append(f'不应发布的文件：{name}')
            continue
        absolute = ROOT/path
        if absolute.is_symlink():
            problems.append(f'本机符号链接：{name}')
            continue
        text = absolute.read_text(encoding='utf-8')
        if re.search(r'/Users/(?!<|USER|example)[A-Za-z0-9_.-]+/', text):
            problems.append(f'本机用户绝对路径：{name}')
        if re.search(r'github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----', text):
            problems.append(f'疑似访问凭据：{name}')
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        return 1
    print(f'发布文件检查通过：{len(list(filter(None, tracked)))} 个跟踪文件，没有人脸资料、图片、模型、日志或本机路径。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
