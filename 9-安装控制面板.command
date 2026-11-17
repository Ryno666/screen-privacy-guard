#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    ./setup.sh || exit 1
fi
./.venv/bin/python install.py
guard_status=$?
if [ "$guard_status" -ne 0 ]; then
    read -r -p '安装未完成，请查看上方错误。按回车关闭。' _
fi
exit "$guard_status"
