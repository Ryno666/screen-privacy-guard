#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    echo '当前安装脚本面向 Apple Silicon Mac。其他平台尚未支持。'
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    guard_python=""
    for candidate in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(not ((3,11) <= sys.version_info[:2] <= (3,13)))' 2>/dev/null; then
                guard_python="$candidate"
                break
            fi
        fi
    done
    if [ -z "$guard_python" ]; then
        echo '需要 Python 3.11–3.13，推荐 Python 3.12。'
        exit 1
    fi
    "$guard_python" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,13), "需要 Python 3.11–3.13，推荐 3.12。"'
guard_hash=$(/usr/bin/shasum -a 256 requirements.txt | /usr/bin/awk '{print $1}')
if [ ! -f .venv/.guard-requirements ] || [ "$(cat .venv/.guard-requirements)" != "$guard_hash" ]; then
    .venv/bin/python -m pip install -r requirements.txt
    printf '%s\n' "$guard_hash" > .venv/.guard-requirements
fi
.venv/bin/python setup_models.py
