@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FunPay Searcher

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден. Установите Python 3.11+ с https://www.python.org/downloads/
    echo     и при установке поставьте галочку "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [*] Первый запуск: создаю виртуальное окружение...
    python -m venv .venv
    if errorlevel 1 ( echo [!] Не удалось создать .venv & pause & exit /b 1 )
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, bs4, lxml, yaml" >nul 2>nul
if errorlevel 1 (
    echo [*] Устанавливаю зависимости...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 ( echo [!] Ошибка установки зависимостей & pause & exit /b 1 )
)

if not exist "config\settings.yaml" copy "config\settings.example.yaml" "config\settings.yaml" >nul

".venv\Scripts\python.exe" -m app %*
pause
