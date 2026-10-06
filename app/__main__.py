"""Точка входа: `python -m app` — запускает локальный сервер и открывает браузер."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from .settings import DATA_DIR, Settings, ensure_user_dirs


def main() -> None:
    parser = argparse.ArgumentParser(description="FunPay Searcher — поиск и перепродажа игровых аккаунтов")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="проверить парсеры FunPay/Lolz, сохранить отчёт в data/diagnostics/ и выйти",
    )
    args = parser.parse_args()

    ensure_user_dirs()
    settings = Settings.load()
    host = args.host or settings.ui.host
    port = args.port or settings.ui.port
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "app.log", encoding="utf-8")],
    )
    if args.diagnose:
        import sys

        from .diagnostics import run_diagnostics, summarize
        from .services.context import AppContext

        print("\n  Диагностика парсеров FunPay / Lolzteam, подождите (до нескольких минут)...\n")
        report = run_diagnostics(AppContext(settings=settings))
        print(summarize(report))
        if getattr(sys, "frozen", False):
            input("\n  Нажмите Enter, чтобы закрыть окно...")
        sys.exit(0 if report.get("ok") else 2)

    # заранее проверяем порт: иначе баннер «запущен» и браузер откроются, а сервер упадёт
    import socket
    import sys

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # TIME_WAIT не считаем занятым
            sock.bind((host, port))
    except OSError as e:
        print(
            f"\n  [!] Порт {port} занят ({e}). Возможно, программа уже запущена.\n"
            f"      Запустите с другим портом (--port {port + 1}) или измените ui.port в config/settings.yaml.\n"
        )
        if getattr(sys, "frozen", False):
            input("  Нажмите Enter, чтобы закрыть окно...")
        sys.exit(3)

    url = f"http://{host}:{port}/"
    print(f"\n  FunPay Searcher запущен: {url}\n  (Ctrl+C — остановить)\n")
    if settings.ui.open_browser and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    from .main import create_app

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
