#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
./setup.sh
guard_status=$?
if [ "$guard_status" -ne 0 ]; then
    read -r -p '准备失败，请查看上方错误。按回车关闭。' _
fi
exit "$guard_status"
