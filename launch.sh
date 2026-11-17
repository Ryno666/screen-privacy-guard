#!/bin/bash
set -uo pipefail
cd -- "$(dirname -- "$0")"
if ! ./setup.sh; then
    echo '准备失败。请查看上面的错误。按回车关闭。'
    read -r _
    exit 1
fi
.venv/bin/python guard.py "$@"
guard_status=$?
if [ "$guard_status" -ne 0 ]; then
    echo '程序已停止。按回车关闭。'
    read -r _
fi
exit "$guard_status"
