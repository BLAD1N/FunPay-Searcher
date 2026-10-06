"""Обёртка для PyInstaller: эквивалент `python -m app`."""
from app.__main__ import main

if __name__ == "__main__":
    main()
