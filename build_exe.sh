#!/usr/bin/env bash
# Сборка FunPay Searcher в один исполняемый файл (PyInstaller) — Linux / macOS.
# Аналог build_exe.bat (там разделитель в --add-data — ";", здесь — ":").
#
# Результат: dist/FunPaySearcher. Положите файл в отдельную папку —
# при первом запуске рядом с ним появятся config/ (настройки, профили) и data/ (база, лог).
#
# Переменные окружения (необязательно):
#   PYTHON   — интерпретатор для создания .venv (по умолчанию python3)
#   DIST_DIR — куда положить готовый файл (по умолчанию dist)
#   WORK_DIR — временные файлы сборки и .spec (по умолчанию build)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PY=${PYTHON:-python3}
DIST_DIR=${DIST_DIR:-dist}
WORK_DIR=${WORK_DIR:-build}

echo "[*] Сборка FunPay Searcher в один исполняемый файл (PyInstaller)..."
if [ ! -x ".venv/bin/python" ]; then
  echo "[*] Создаю виртуальное окружение .venv..."
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q -r requirements.txt pyinstaller

# Пути к данным — абсолютные: PyInstaller считает относительные пути от папки .spec-файла (WORK_DIR).
.venv/bin/python -m PyInstaller --noconfirm --clean --onefile --name FunPaySearcher \
  --distpath "$DIST_DIR" --workpath "$WORK_DIR" --specpath "$WORK_DIR" \
  --paths "$ROOT" \
  --add-data "$ROOT/app/web/static:app/web/static" \
  --add-data "$ROOT/config/settings.example.yaml:config" \
  --add-data "$ROOT/config/profiles:config/profiles" \
  --collect-submodules app \
  --collect-submodules uvicorn \
  --hidden-import bs4.builder._lxml \
  --hidden-import lxml.etree --hidden-import lxml._elementpath \
  "$ROOT/run_app.py"

echo
echo "[OK] Готово: $DIST_DIR/FunPaySearcher"
echo "     Положите файл в отдельную папку — рядом с ним появятся config/ и data/."
