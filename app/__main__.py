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
    url = f"http://{host}:{port}/"
    print(f"\n  FunPay Searcher запущен: {url}\n  (Ctrl+C — остановить)\n")
    if settings.ui.open_browser and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    from .main import create_app
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
