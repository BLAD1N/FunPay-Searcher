# FunPay Searcher — заметки для разработки

Локальное приложение (FastAPI + vanilla JS) для дропшиппинга игровых аккаунтов: поиск на FunPay/Lolzteam
по профилям, создание лотов на FunPay с наценкой, мониторинг исходников, заказы, репрайсинг, автоответчик,
Telegram-уведомления. Язык пользователя — русский: тексты UI, комментарии, сообщения журнала на русском.

## Команды
- Окружение: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest ruff`
- Запуск: `python -m app --no-browser --port 8787` (UI на http://127.0.0.1:8787, API-доки /api/docs)
- Тесты без браузера: `pytest -q -p no:warnings --ignore=tests/e2e` (~5 с)
- E2E (Playwright, Chromium из `/opt/pw-browsers/chromium-*`): `pytest tests/e2e -q -p no:warnings` (~15 с)
- Линт: `ruff check . && ruff format --check .`
- Сборка exe: `build_exe.bat` / `./build_exe.sh` (см. docs/BUILD.md)

## Архитектура (контракты)
- `app/models.py` — все общие модели (Listing, Found, OurLot, Order, Profile, Criteria, PricingRule…). Меняются осторожно: от них зависят все модули.
- `app/settings.py` — настройки (config/settings.yaml, пример config/settings.example.yaml — держать синхронно).
- `app/storage.py` — SQLite, единственное соединение под RLock; все методы потокобезопасны.
- `app/sources/funpay.py` — парсинг FunPay (cookie golden_key). Нет сети в CI: тесты на фикстурах tests/fixtures/funpay/.
- `app/sources/lolz.py` — Lolzteam Market API (Bearer token, пауза 3 с между запросами).
- `app/matching.py` / `app/templating.py` / `app/pricing.py` — критерии, шаблоны лота, наценка (чистые функции).
- `app/services/*` — поиск, публикация, монитор (поток), заказы, автоподнятие, репрайсинг, автоответчик (поток).
- `app/main.py` — HTTP API; тела запросов — модели уровня модуля (из-за `from __future__ import annotations`).
- `app/web/static/` — SPA без сборки; e2e-тесты завязаны на тексты кнопок и селекторы.

## Правила
- Никогда не публиковать лот без определения его id на FunPay повторно (дубль). POST /lots/offerSave — без ретраев.
- Секреты (golden_key, токены) не попадают в журнал, экспорт, Telegram и ответы API (см. Settings.masked()).
- Любой парсер FunPay оборачивает поиск элементов и бросает SourceError с русским текстом, не AttributeError.
- Изменяющие запросы к /api принимаются только со своего origin (middleware в main.py).
- Новые фоновые задачи — в MonitorService._loop с интервалами из настроек; никогда не ронять поток.
