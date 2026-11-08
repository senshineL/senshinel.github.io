#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  ./setup.command
fi
# 保持终端开启。抑制空闲睡眠；无法在关机、合盖睡眠或断网时继续查询。
exec /usr/bin/caffeinate -i .venv/bin/python hainan_watch.py watch
