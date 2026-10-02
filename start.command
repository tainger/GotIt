#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi, uvicorn, playwright' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r app/requirements.txt
fi
echo '正在启动 GotIt，启动完成后请打开 http://127.0.0.1:8765'
echo '关闭此窗口或按 Ctrl+C 停止。'
exec .venv/bin/python -m uvicorn gotit.server:app --app-dir app --host 127.0.0.1 --port 8765 --no-access-log
