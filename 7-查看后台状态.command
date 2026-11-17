#!/bin/bash
"$(dirname -- "$0")/.venv/bin/python" "$(dirname -- "$0")/service.py" status
read -r -p '按回车关闭。' _
