@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [*] Сборка FunPay Searcher в один exe-файл (PyInstaller)...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --name FunPaySearcher ^
  --add-data "app\web\static;app\web\static" ^
  --add-data "config\settings.example.yaml;config" ^
  --add-data "config\profiles;config\profiles" ^
  --collect-all uvicorn --hidden-import app.notify --hidden-import app.services.orders --hidden-import app.services.raiser ^
  run_app.py
if errorlevel 1 ( echo [!] Сборка не удалась & pause & exit /b 1 )
echo.
echo [OK] Готово: dist\FunPaySearcher.exe
echo      Положите exe в отдельную папку — рядом с ним появятся config\ и data\.
pause
