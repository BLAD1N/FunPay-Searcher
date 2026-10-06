# Как «обучить» поиск: профили, критерии, наценка, шаблоны

Профиль — это один файл `config/profiles/<id>.yaml` (или карточка в разделе **Профили** интерфейса).
Один профиль = одна игра + регион + набор критериев + правило наценки + шаблон лота.

Эталонный профиль с комментариями к каждому полю — `wot_ru_tops.yaml`. Справочник **всех** опций
с пояснениями прямо в файле — `any_game_cheap_tops.yaml` (выключен, предназначен для копирования).

## 0. Готовые профили

| id | Игра | Регион | Источники | Включён | Суть критериев |
|---|---|---|---|---|---|
| `wot_ru_tops` | World of Tanks / Мир танков | RU (Lesta) | FunPay, Lolz `world-of-tanks` | **да** | все топы / десятки, наградная техника (Chieftain, Об. 279 (р), Об. 260, Об. 907, Carro 45t, Kpz 07 RH, 121B, M60, VK 72.01 K), коллекционеры; 3 000–60 000 ₽ |
| `wot_eu_tops` | World of Tanks | EU (Wargaming) | FunPay, Lolz `world-of-tanks` | нет | то же + английские формулировки (all tier 10, reward tanks); отсекает Lesta/RU |
| `wot_na_tops` | World of Tanks | NA | FunPay, Lolz `world-of-tanks` | нет | как EU |
| `wot_asia_tops` | World of Tanks | ASIA/SEA | FunPay, Lolz `world-of-tanks` | нет | как EU |
| `wot_blitz` | WoT Blitz / Tanks Blitz | любой | FunPay, Lolz `wot-blitz` | нет | все топы, 50+ танков, легендарные камуфляжи, коллаборации, наградная техника Blitz (Smasher, Gravedigger, Helsing...); 1 000–30 000 ₽ |
| `dota2_high_mmr` | Dota 2 | любой | FunPay, Lolz `steam` (game 570) | **да** | числовой MMR ≥ 6000 (regex), Immortal/Divine, арканы (Dragonclaw, Golden), Battle Pass, лидерборд; VAC — отказ; 2 000–80 000 ₽ |
| `cs2_prime` | CS2 | любой | FunPay, Lolz `steam` (game 730) | **да** | Prime, FACEIT lvl, Premier, ножи/перчатки, медали (5/10 Year Coin, Service Medal), дорогой инвентарь; VAC/trade ban/red trust — отказ; 1 000–50 000 ₽ |
| `fortnite_skins` | Fortnite | любой | FunPay, Lolz `fortnite` | нет | OG-скины (Renegade Raider, Black Knight, Galaxy, IKONIK, Glow, Wonder, Travis Scott, Minty, Aerial Assault Trooper...), 1–2 сезон, 100+ скинов, STW; 1 000–40 000 ₽ |
| `genshin` | Genshin Impact | любой | FunPay, Lolz `mihoyo` | нет | AR 55–60 или 5★ (regex), лимитные 5★ (Ху Тао, Райден, Нахида, Фурина, Нёвиллет...), C6/R5; реролл/стартовые — отказ; 1 500–40 000 ₽ |
| `valorant` | Valorant | любой | FunPay, Lolz `riot` | нет | Immortal/Radiant/Ascendant, коллекции (Reaver, Prime, Elderflame, Champions, Glitchpop...), ножи, старые аккаунты; смурфы — отказ; 1 000–40 000 ₽ |
| `pubg` | PUBG: Battlegrounds (ПК) | любой | FunPay, Lolz `steam` (game 578080) | нет | редкие сеты (Pajama, Bunny, Trenchcoat, School, Zest), M416 Glacier, ранг, 1000+ часов, старые аккаунты; PUBG Mobile/New State — отказ; 1 500–40 000 ₽ |
| `apex` | Apex Legends | любой | FunPay, Lolz `ea` (или `steam` 1172470) | нет | Heirloom, Prestige, Predator/Master/Diamond, значки 20 kills / 4k, все легенды, 500 lvl; 1 500–40 000 ₽ |
| `brawl_stars` | Brawl Stars | любой | FunPay, Lolz `supercell` | нет | кубки ≥ 10k (regex `(\d{2,3})\s*(k|к|тыс)\s*(кубк|trophies)`), легендарки (Спайк, Ворон, Леон, Сэнди, Амбер...), гиперзаряды, ранг 35; без Supercell ID — отказ; 500–30 000 ₽ |
| `clash_royale` | Clash Royale | любой | FunPay, Lolz `supercell` | нет | уровень короля 14–16 / 50+, кубки ≥ 7000 или Ultimate Champion (regex), эволюции, все легендарки/чемпионы, макс колода; 500–20 000 ₽ |
| `standoff2` | Standoff 2 | любой | **только FunPay** | нет | голда ≥ 10k (regex), ножи (бабочка, керамбит, M9, Fang...), перчатки, Легенда/Феникс; продажа голды — отказ; 500–30 000 ₽ |
| `roblox` | Roblox | любой | FunPay, Lolz `roblox` | нет | Robux ≥ 1000 / RAP ≥ 10k (regex), Headless/Korblox/Dominus/Valkyrie, лимитки, Blox Fruits / MM2 / Adopt Me; PIN, детские аккаунты, продажа робаксов — отказ; 300–30 000 ₽ |
| `eft` | Escape from Tarkov | любой | FunPay, Lolz `escape-from-tarkov` | нет | EOD / Unheard / PFE, Каппа, Lightkeeper, уровень 40+, макс торговцы/убежище; ключи активации, апгрейды, Arena Breakout — отказ; 1 500–40 000 ₽ |
| `warface` | Warface (ПК) | любой | FunPay, Lolz `warface` | нет | ранг ≥ 50 или донат/золотое/вечное оружие (regex), VIP, наборы; мобайл/консоли, продажа кредитов — отказ; 500–20 000 ₽ |
| `lol` | League of Legends | любой | FunPay, Lolz `riot` | нет | все чемпионы, Victorious/Prestige/Ultimate/PAX/Hextech, Diamond+, 100+ скинов, старые аккаунты; смурфы, Wild Rift — отказ; 1 000–40 000 ₽ |
| `steam_inventory` | Steam (любая игра) | любой | FunPay `Steam`, Lolz `steam` | нет | стоимость инвентаря ≥ 5 000 ₽ / 50 $ (regex + `numeric` по `inv_value`), ножи/перчатки/арканы/Unusual, уровень Steam, стаж; холд, VAC — отказ; 2 000–100 000 ₽ |
| `any_game_cheap_tops` | шаблон (любая игра) | любой | FunPay, Lolz | нет | справочник всех опций; «дешёвые топы» до 5 000 ₽ с наценкой ступенями + `price_ending: 990` |

Включены по умолчанию только три профиля с самой проверенной выдачей (WoT RU, Dota 2, CS2).
Остальные включайте после проверки категорий FunPay (кнопка «Категории») и параметров Lolz
(кнопка «Параметры»): имена параметров категорий Lolz в файлах помечены «проверьте через Параметры».

В каждом файле есть блок `examples: {positive: [...], negative: [...]}` — тесты `tests/test_profiles.py`
прогоняют их через матчинг. Модель профиля этот ключ игнорирует, при сохранении из UI он теряется, поэтому
правьте примеры в файле. Добавили профиль — добавьте примеры.

## 1. Где искать

```yaml
sources:
  funpay:
    enabled: true
    game_query: "World of Tanks"      # игра по названию (точное -> по началу -> подстрока)
    subcategory_query: "Аккаунты"     # подкатегория по названию
    # subcategory_id: 148             # либо точный id узла /lots/148/ (выбирается в UI кнопкой «Выбрать категорию»)
    server_filter: ["RU", "Lesta"]    # оставить только объявления, где сервер/регион содержит одну из подстрок
    max_items: 500
  lolz:
    enabled: true
    category: world-of-tanks          # steam, fortnite, mihoyo, riot, world-of-tanks, wot-blitz, supercell, ea, roblox ...
    params:                           # документированные: order_by, title, pmin/pmax (берутся из criteria.price), game[]
      order_by: price_to_up
      # game: [570]                   # для steam: 570 = Dota 2, 730 = CS2, 578080 = PUBG
      # tanks_min: 30                 # параметры категории — проверьте через «Параметры категории» в UI
    pages: 3
```

Если у игры несколько похожих названий на FunPay (PUBG / PUBG Mobile / PUBG New State) — задайте
`subcategory_id` явно. Если на Lolz нет категории (Standoff 2) — выключите источник: `lolz: {enabled: false}`.

## 2. Критерии отбора

```yaml
criteria:
  price: { min: 5000, max: 40000 }     # в рублях
  must_any: ["все топы", "все 10", "chieftain|чифтейн", "об 279"]   # хотя бы одно
  must_all: []                          # все обязательны
  exclude: ["забанен*", "без почты", "в аренду"]                   # стоп-слова
  regex_any: ['(\d{4,5})\s*(mmr|ммр)']  # регулярные выражения (хотя бы одно)
  regex_exclude: []
  numeric:                              # числовые поля из attributes (Lolz отдаёт их структурно)
    - { field: wot_tanks_count, min: 50 }
  highlights: ["Chieftain => chieftain|чифт*", "Об. 279 (р)", "Об. 260", "ИС-7", "60TP"]
  regions: ["RU"]                       # RU / EU / NA / ASIA (синонимы: россия, lesta, европа, америка, азия ...)
  min_score: 1
  seller_min_reviews: 10
```

Синтаксис слов (одинаков для `must_any`, `must_all`, `exclude`, `highlights`):

| Запись | Что значит |
|---|---|
| `чифтейн` | слово целиком, регистр не важен, `ё` = `е`, латинские `a c e o p x y` = кириллические |
| `chieftain\|чифтейн` | любой из вариантов |
| `топ*` | любое продолжение слова: топы, топовые, топчик |
| `все топы` | фраза; знаки препинания между словами игнорируются (`Об. 279` найдёт `об279`, `об 279 (р)`) |
| `Chieftain => chieftain\|чифт*` | слева — как показывать в заголовке лота, справа — что искать |

Как считается балл: +1 за каждое совпавшее слово из `must_any`/`must_all`, +1 за регулярку, +1 за числовое
правило, +0.5 за каждый highlight. Объявление проходит, если нет ни одного отклонения (цена, регион,
стоп-слова, отсутствие обязательных слов) и балл ≥ `min_score`.

Кнопка **«Проверить критерии»** в редакторе профиля показывает по любому тексту, что совпало и почему
отклонено — так удобно подбирать слова.

### Как правильно подбирать слова

Правило профессионального перекупа: **`must_any` — только маркеры качества, `exclude` — только то, что
точно дисквалифицирует.** Вот ложные срабатывания, которые были в стартовых профилях и которые мы убрали.

**Слишком общие слова в `must_any`** (есть в каждом объявлении — профиль пропускает всё подряд):

| Было | Почему плохо | Стало |
|---|---|---|
| `аккаунт`, `акк`, `account` | есть везде | убрано; пишем конкретику: «все топы», «Chieftain» |
| `прем`, `премиум` | «прем 3 дня» — не ценность; «премиум» ≠ Roblox Premium | `прем* танк*\|премы\|премов`; для Roblox `roblox premium\|премиум подписк*` |
| `скин*`, `skin*`, `инвентар*` (CS2, Fortnite, Valorant) | в любом объявлении | конкретные скины (Renegade Raider, Reaver), «дорог* инвентар*», «100 скин*» |
| `рейтинг\|rating` (Dota 2) | «рейтинг продавца», «рейтинг 2000» | числовой MMR через `regex_any` |
| `редк*`, `rare`, `лимит*` | «редко захожу», «лимит на...» | названия предметов |
| `голд*` (WoT Blitz, Standoff 2) | голда есть на любом счёте | в WoT — только `highlights`; в SO2 — `regex_any` с количеством ≥ 10k |
| `coin`, `VP`, `бп`, `ge`, `lb`, `бф` (2–3 буквы) | совпадают с чем угодно | убраны или заменены на полные формы |
| `Titan` (Dota 2) | такого ранга нет | убрано |

**Опасные стоп-слова в `exclude`** (отсекают хорошие объявления):

| Было | Что отсекало | Стало |
|---|---|---|
| `бан\|ban` | «без бана», «no ban», «банов нет» | `забанен*\|banned\|в бане\|есть бан\|с баном\|пермабан` + `regex_exclude` `(есть\|стоит\|with\|has)\s+(бан\|ban)` |
| `vac` | «без VAC», «VAC: нет», «no vac ban» | регулярка с отрицаниями: `(?<!no )(?<!без )(?<!нет )\b(vac\|вак)(?!…(нет\|no\|none\|0))` — голое «VAC» всё ещё отказ |
| `обмен` | «обмен доступен» = трейд разрешён (плюс для Steam) | `только обмен\|на обмен\|обменяю\|trade only` |
| `прокачка`, `буст*` | «прокачка ангара 90 %», «бустеры» в инвентаре | `прокачаю\|прокачка на заказ\|буст\|boosting` |
| `без привязки` | «без привязки к телефону» — это плюс | `без привязки почты\|без привязки к почте\|без почты` |
| `временно` | «временно снижена цена» | `временн* доступ*\|временн* аккаунт*\|аренда\|rent` |
| `чит*` | «читайте описание» | `чит\|читы\|читами\|cheat*\|hack*` |
| `ключ\|key` | ключи от кейсов (CS2), ключи от общаги (Tarkov) | `ключ активации\|game key\|код активации` |
| `холд` | «без холда» | `в холде\|на холде\|с холдом\|trade hold 7` |
| `чист* аккаунт*` | «чистый» = без банов | убрано; стартовые аккаунты ловим по `смурф*\|стартов*\|fresh account` |
| `пин\|pin` (Roblox) | «без пина» | `с пином\|pin не снят\|pin включен` |
| `<13`, `13+ лет` | после нормализации — голое «13», совпадает с чем угодно | убрано |

**Другие пригодившиеся приёмы**

* Другая игра в той же категории: Blitz в WoT, PUBG Mobile в PUBG, Wild Rift в LoL, Arena Breakout в Tarkov,
  Null's Brawl в Brawl Stars — стоп-слова по названию.
* Продажа валюты/услуг вместо аккаунта: «по курсу», «пополнение», «gift card», «ключ активации», «апгрейд
  до EOD», «общий аккаунт / family sharing», «на ваш аккаунт».
* Числа внутри слов: `все 10` не найдёт «все 100», а `50+ топов` после нормализации = «50 топов», поэтому
  `50 топов|60 топов|...` в одном условии. Всё, что требует диапазона чисел (MMR ≥ 6000, кубки ≥ 10k),
  делайте через `regex_any` — но помните, что непустой `regex_any` **обязателен** (это ещё один фильтр «И»).
* Подписи `=>` держите на языке заголовка лота и одинаковыми в `must_any` и `highlights` — дубликаты фишек
  в заголовок не попадают.
* Тест `test_excludes_do_not_contradict_positives` ловит самопротиворечия: вариант из `must_any`/`highlights`,
  который сам срабатывает как стоп-слово.

## 3. Наценка

```yaml
pricing:
  mode: formula              # percent | multiplier | formula | tiers
  formula: "price * 2 - 1000"   # 15 000 -> 29 000, 30 000 -> 59 000
  percent: 90                # для mode: percent
  multiplier: 1.9            # для mode: multiplier
  tiers:                     # для mode: tiers (ступени по цене закупки)
    - { up_to: 10000, percent: 100 }
    - { up_to: 50000, percent: 90 }
  min_margin: 2000           # минимум наценки в рублях
  round_to: 100              # округление
  round_mode: nearest        # nearest | down | up
  price_ending: 990          # необязательно: 28 990 вместо 29 000
```

В формуле доступны `price`, арифметика, `min/max/round/floor/ceil` и условия:
`price * 2 if price < 20000 else price * 1.7`. Цена никогда не опускается ниже закупки.
Для дешёвых игр (Brawl Stars, Clash Royale, Standoff 2, Roblox, Warface) в профилях стоит `tiers`
с бóльшим процентом на дешёвые аккаунты.

## 4. Шаблон лота («наш стиль»)

```yaml
lot_template:
  funpay_subcategory_id: null        # куда публиковать; пусто = туда же, где искали
  title_ru: "🔥 {game_short} {region} | {highlights} | Полный доступ, смена почты"
  title_en: ""
  description_ru: |
    🔥 {game} — аккаунт с топовой техникой
    📦 Что внутри:
    {highlights_lines}
    🛡 Гарантия чистоты, полная передача, смена почты.
    💬 Пишите перед покупкой — отвечаем быстро.
  amount: 1
  deactivate_after_sale: true
  active: true
  fields:                            # доп. поля формы FunPay (кнопка «Загрузить поля формы FunPay»)
    "fields[server]": "ru"
```

Плейсхолдеры: `{game}`, `{game_short}`, `{region}`, `{highlights}` (через « • »), `{highlights_lines}`
(по строке с ✅), `{price}` (29 000), `{price_raw}`, `{source_price}`, `{source_title}`, `{source_description}`,
`{seller}`, `{source}`, `{profile_name}`, `{currency}`, `{attr[имя_поля]}` — любое поле из `attributes`
(например `{attr[steam_level]}`). В стартовых профилях `{seller}`, `{source}`, `{source_title}`,
`{source_description}` не используются и тесты это проверяют — покупатель не должен видеть, откуда аккаунт.

Все профили оформлены в едином стиле «премиум-магазина»: эмодзи-заголовок, блок «📦 Что внутри» из
`{highlights_lines}`, «🛡 Гарантии магазина», «🚚 Доставка», призыв написать перед покупкой. Заголовок
автоматически укладывается в 100 символов: сначала убираются фишки с конца, потом текст режется по слову.

## 5. Типовые примеры

* **Танки, все топы на RU** — `must_any: ["все топы", "все 10", "топ* танк*"]`, `regions: ["RU"]`,
  `highlights` — список культовых машин.
* **Dota 2, высокий MMR** — `regex_any: ['([6-9]\d{3}|1\d{4})\s*(mmr|ммр)']`, `must_any: ["immortal|имморт*", "аркан*|arcana"]`.
* **CS2 Prime с инвентарём** — `must_any: ["prime|прайм"]`, `exclude: ["забанен*", "red trust"]`,
  `regex_exclude` на VAC/trade ban, `numeric: [{field: inv_value, min: 5000}]` (имя поля смотрите в `attributes["raw_keys"]`
  находки с Lolz).
* **Fortnite OG-скины** — `must_any: ["renegade", "black knight", "galaxy", "ikonik", "og"]`, `numeric: [{field: fortnite_skin_count, min: 50}]`.
* **Brawl Stars 10k+ кубков** — `regex_any: ['(\d{2,3})\s*(k|к|тыс)\s*(кубк|trophies)']`, `must_any: ["spike|спайк", "crow|ворон", "leon|леон"]`.
