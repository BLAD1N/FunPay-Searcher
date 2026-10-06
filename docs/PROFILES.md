# Как «обучить» поиск: профили, критерии, наценка, шаблоны

Профиль — это один файл `config/profiles/<id>.yaml` (или карточка в разделе **Профили** интерфейса).
Один профиль = одна игра + регион + набор критериев + правило наценки + шаблон лота.

## 1. Где искать

```yaml
sources:
  funpay:
    enabled: true
    game_query: "World of Tanks"      # игра по названию (как на FunPay)
    subcategory_query: "Аккаунты"     # подкатегория по названию
    # subcategory_id: 148             # либо точный id узла /lots/148/ (выбирается в UI кнопкой «Выбрать категорию»)
    server_filter: ["RU", "Lesta"]    # оставить только объявления, где сервер/регион содержит одну из подстрок
    max_items: 500
  lolz:
    enabled: true
    category: world-of-tanks          # steam, fortnite, mihoyo, riot, world-of-tanks, wot-blitz, epicgames, supercell ...
    params:                           # любые параметры API Lolz; список — кнопка «Параметры категории» в UI
      order_by: price_to_up
      # game: [570]                   # для steam: 570 = Dota 2, 730 = CS2
    pages: 3
```

## 2. Критерии отбора

```yaml
criteria:
  price: { min: 5000, max: 40000 }     # в рублях
  must_any: ["все топы", "все 10", "chieftain|чифтейн", "об 279"]   # хотя бы одно
  must_all: []                          # все обязательны
  exclude: ["бан", "заблокирован", "без почты"]                      # стоп-слова
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
`price * 2 if price < 20000 else price * 1.7`.

## 4. Шаблон лота («наш стиль»)

```yaml
lot_template:
  funpay_subcategory_id: null        # куда публиковать; пусто = туда же, где искали
  title_ru: "{game} | {highlights} | {region}"
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
(например `{attr[steam_level]}`). Описание по умолчанию никогда не раскрывает ссылку на исходник и продавца.

## 5. Типовые примеры

* **Танки, все топы на RU** — `must_any: ["все топы", "все 10", "топ* танк*"]`, `regions: ["RU"]`,
  `highlights` — список культовых машин.
* **Dota 2, высокий MMR** — `regex_any: ['([6-9]\d{3}|1\d{4})\s*(mmr|ммр)']`, `must_any: ["immortal|имморт*", "аркан*|arcana"]`.
* **CS2 Prime с инвентарём** — `must_any: ["prime|прайм"]`, `must_all: []`, `exclude: ["vac", "trade ban", "бан"]`,
  `numeric: [{field: inv_value, min: 5000}]` (имя поля смотрите в `attributes` находки с Lolz).
* **Fortnite OG-скины** — `must_any: ["renegade", "black knight", "galaxy", "ikonik", "og"]`, `numeric: [{field: fortnite_skin_count, min: 50}]`.
