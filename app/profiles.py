"""Загрузка/сохранение профилей поиска из config/profiles/*.yaml."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import Profile
from .settings import PROFILES_DIR

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")


class ProfileStore:
    def __init__(self, directory: Path = PROFILES_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, profile_id: str) -> Path:
        if not _SAFE_ID.match(profile_id):
            raise ValueError(f"Недопустимый id профиля: {profile_id!r}")
        return self.directory / f"{profile_id}.yaml"

    def list(self) -> list[Profile]:
        result: list[Profile] = []
        for p in sorted(self.directory.glob("*.yaml")):
            try:
                result.append(self.load_file(p))
            except Exception as e:  # noqa: BLE001 — показываем битые профили в логе, но не падаем
                print(f"[profiles] не удалось прочитать {p.name}: {e}")
        return result

    def load_file(self, path: Path) -> Profile:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("id", path.stem)
        return Profile.model_validate(data)

    def get(self, profile_id: str) -> Profile:
        path = self._path(profile_id)
        if not path.exists():
            raise KeyError(profile_id)
        return self.load_file(path)

    def save(self, profile: Profile) -> Profile:
        path = self._path(profile.id)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(profile.model_dump(mode="json", exclude_none=True), f,
                           allow_unicode=True, sort_keys=False, width=120)
        return profile

    def delete(self, profile_id: str) -> None:
        path = self._path(profile_id)
        if path.exists():
            path.unlink()
