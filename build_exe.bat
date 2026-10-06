@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [*] Сборка FunPay Searcher в один exe-файл (PyInstaller)...

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден. Установите Python 3.11+ с https://www.python.org/downloads/
    echo     и при установке поставьте галочку "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [*] Создаю виртуальное окружение .venv...
    python -m venv .venv
    if errorlevel 1 ( echo [!] Не удалось создать .venv & pause & exit /b 1 )
)

".venv\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 ( echo [!] Не удалось установить зависимости & pause & exit /b 1 )

rem Пути к данным — абсолютные: PyInstaller считает относительные пути от папки .spec-файла (build\).
rem Разделитель в --add-data на Windows — ";" (в build_exe.sh для Linux/macOS — ":").
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --name FunPaySearcher ^
  --distpath dist --workpath build --specpath build ^
  --paths "%~dp0." ^
  --add-data "%~dp0app\web\static;app\web\static" ^
  --add-data "%~dp0config\settings.example.yaml;config" ^
  --add-data "%~dp0config\profiles;config\profiles" ^
  --collect-submodules app ^
  --collect-submodules uvicorn ^
  --hidden-import bs4.builder._lxml ^
  --hidden-import lxml.etree --hidden-import lxml._elementpath ^
  "%~dp0run_app.py"
if errorlevel 1 ( echo [!] Сборка не удалась & pause & exit /b 1 )

echo.
echo [OK] Готово: dist\FunPaySearcher.exe
echo      Положите exe в отдельную папку — рядом с ним появятся config\ и data\.
echo      Подробнее: docs\BUILD.md
pause
