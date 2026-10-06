"""Базовый интерфейс источника объявлений."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Listing, Profile


class SourceError(Exception):
    """Ошибка источника (сеть, авторизация, разбор страницы)."""


class AuthError(SourceError):
    """Невалидный токен / cookie."""


class BaseSource(ABC):
    name: str = "base"

    @abstractmethod
    def search(self, profile: Profile, limit: Optional[int] = None) -> list[Listing]:
        """Найти объявления по настройкам профиля (без применения критериев — это делает matching)."""

    @abstractmethod
    def get_listing(self, source_id: str) -> Optional[Listing]:
        """Загрузить одно объявление по id. None — объявление удалено/не найдено."""

    @abstractmethod
    def is_available(self, source_id: str) -> Optional[bool]:
        """True — доступно, False — продано/снято, None — не удалось проверить."""

    @abstractmethod
    def check_auth(self) -> dict:
        """Проверить доступ (токен/cookie). Возвращает словарь с информацией об аккаунте."""
