#!/usr/bin/env bash
# Запуск FunPay Searcher на Linux / macOS
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x ".venv/bin/python" ]; then
  echo "[*] Первый запуск: создаю виртуальное окружение..."
  "$PY" -m venv .venv
fi
if ! .venv/bin/python -c "import fastapi, uvicorn, httpx, bs4, lxml, yaml" 2>/dev/null; then
  echo "[*] Устанавливаю зависимости..."
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi
[ -f config/settings.yaml ] || cp config/settings.example.yaml config/settings.yaml
exec .venv/bin/python -m app "$@"
