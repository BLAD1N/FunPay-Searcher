"""Обёртка для PyInstaller: эквивалент `python -m app`."""
import multiprocessing

from app.__main__ import main

if __name__ == "__main__":
    # Windows + PyInstaller (--onefile): без этого дочерний процесс multiprocessing
    # (если он когда-нибудь появится) повторно запустил бы весь сервер.
    multiprocessing.freeze_support()
    main()
