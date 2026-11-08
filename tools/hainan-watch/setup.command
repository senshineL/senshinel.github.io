#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
if [[ ! -d '/Applications/Microsoft Edge.app' && ! -d '/Applications/Google Chrome.app' ]]; then
  .venv/bin/python -m playwright install chromium
fi
.venv/bin/python - <<'PY'
import json
from pathlib import Path
path = Path('config.json')
config = json.loads(path.read_text())
if Path('/Applications/Microsoft Edge.app').exists():
    config['browser_channel'] = 'msedge'
elif Path('/Applications/Google Chrome.app').exists():
    config['browser_channel'] = 'chrome'
else:
    config['browser_channel'] = 'chromium'
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
PY
print '安装完成。运行 .venv/bin/python hainan_watch.py test-notification 测试 Mac 通知，然后运行 ./start.command。'
