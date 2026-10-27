# Публичный кейс Tratte — synthetic demo

[Кейс](https://shchukins.github.io/tratte/case.html) →
[интерактивное demo](https://shchukins.github.io/tratte/demo.html).
Все записи придуманы генератором, а не получены обезличиванием реальных чеков.
Никакой личной БД, Gmail OAuth, Telegram, LLM или внешних секретов не требуется.

## Самый короткий запуск

Из корня checkout, Python 3.12+ (для страницы — любой Python 3):

```bash
python -m analytics.demo.build
python -m http.server 8767 --bind 127.0.0.1 --directory docs
# http://127.0.0.1:8767/case.html
# http://127.0.0.1:8767/demo.html
```

Только стандартная библиотека. Страница не обращается к серверу аналитики,
базе данных или CDN; браузер читает локальную fixture JSON. Нужен HTTP, а не file://.
Кнопки показывают факторы и наблюдения фиксированной пары месяцев. Это публичная
иллюстрация проверенного расчёта, не замена Superset и не редактор scope.

`build.py` использует локальный Random с seed **20261027**; создаёт три месяца,
два вымышленных магазина и 34 записи. Порядок и технические ID стабильны.
`docs/assets/case/data.json` — единственный публичный набор наблюдений.
`expected.json` — написанные вручную контрольные итоги, не вывод production-функций.
Результаты за август/сентябрь совпадают с проверенным demo #6. Июль добавлен
для сценария неполных данных. Скриншоты взяты из фактического Superset #6;
их snapshot/item ID не являются ID новой fixture. В #6 использовались единицы
упаковок; новая fixture подтверждает 1 л / 1 кг и переводит их в физические
единицы. Для этих размеров значения количеств и цены совпадают.

## Полный путь через SQL и Superset

Нужны Python 3.12+, зависимости проекта и Docker с PostgreSQL 16.
Следующие команды создают **отдельный новый контейнер** на loopback:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
docker run -d --name tratte-public-demo \
  -e POSTGRES_PASSWORD=synthetic-demo -e POSTGRES_DB=demo \
  -p 127.0.0.1:55437:5432 postgres:16-alpine
# Дождитесь: docker exec tratte-public-demo pg_isready -U postgres
export TRATTE_DEMO_DATABASE_URL='postgresql+psycopg://postgres:synthetic-demo@localhost:55437/demo'
python -m analytics.demo.postgres
```

Пароль выше — публичный одноразовый demo пароль. Не применять его для рабочих
данных и не использовать URL production. Загрузчик отказывает при существующих
таблицах или пользовательских схемах: повтор требует новой пустой demo БД.
Миграции создают схему только внутри этого контейнера; source production не читается.
Отсутствующий итог июля сохранён как Receipt.total=NULL без выдуманной строки товара.

Команда применяет подтверждённый synthetic mapping (упаковки 1 л / 1 кг), вызывает
**те же** `calculate(..., dashboard=True)` и `publish`, что сервис, и проверяет
итоги, каждый компонент моста, товарные факторы, охват и наблюдения по независимому
`expected.json`. Создаёт `output/demo/report.json` и привязанный к точному snapshot
`output/demo/superset.zip`. Метаданные времени/версии mapping/snapshot меняются
между запусками; записи fixture и контрольная арифметика остаются одинаковыми.

Для визуального запуска потребуется отдельный Superset **6.1.0**, его metadata DB
и настройка reader-соединения. [Инструкция импорта и прав](../superset/README.md).
В ZIP URI всегда placeholder, не рабочее подключение. Для Superset в Docker адрес
`localhost` указывает на сам контейнер: соедините demo PostgreSQL и Superset
отдельной Docker network и укажите имя demo контейнера. Не открывайте production
BI ради demo. Нативный проверенный экспорт #6: [tratte-expenses.zip](../superset/tratte-expenses.zip).
Новый `output/demo/superset.zip` строится тем же генератором, но привязан к новой fixture.

После проверки удалите только созданное окружение:

```bash
docker rm -f tratte-public-demo
```

Стоимость: публичная страница статическая, постоянно работающий Superset не нужен.
Локальному JSON demo достаточно браузера и Python; SQL-проверке нужны зависимости,
PostgreSQL и диск под Docker image. Superset дополнительно требует свой контейнер,
metadata DB, память и ручную настройку импорта/reader. Время установки зависит от
кеша образов и сети; замеры универсальных минут или рублей не заявляются.

## Контрольная арифметика и словарь

| Метрика | Определение / demo |
|---|---|
| Расход | Сумма итогов RUB-чеков операции purchase: 850 → 1 410 ₽ |
| Изменение | 1 410 − 850 = +560 ₽; относительно базы +65,9% |
| S, Q, P | Сумма строк, сопоставимое количество, P = S/Q |
| Цена | (P₁ − P₀) × (Q₁ + Q₀)/2: молоко +150, рис −50, всего +100 ₽ |
| Количество | (Q₁ − Q₀) × (P₁ + P₀)/2: молоко +230, рис +190, всего +420 ₽ |
| Вход / выход | Новый товар +70 ₽, исчезнувший −30 ₽; не ценовой эффект |
| Unmatched | Неоднозначная смесь: по 20 ₽, разность 0 ₽, не инфляция |
| Сверка | Изменение разности сумм чеков и строк: 0 ₽ |
| Округление | Явный остаток округления компонентов: 0 ₽ |
| Охват | Сопоставимые положительные строки / все положительные RUB purchase строки: 800/850 = 94,1%; 1320/1410 = 93,6% |
| Медиана | Отдельное описание цен наблюдений; не замена S/Q в факторах |
| Исключения | Unknown operation: по 10 ₽; в расход purchase не входит |
| Свежесть | Последняя завершённая синхронизация: N/A в synthetic источнике |
| Полнота | Июль без итога чека: полный мост N/A / insufficient_data |

Молоко 1 л: S=400→780 ₽, Q=8→12 л, P=50→65 ₽/л; Δ=+380 ₽.
Рис 1 кг: S=400→540 ₽, Q=8→12 кг, P=50→45 ₽/кг; Δ=+140 ₽.
Для каждого — 8 наблюдений / 4 медианы month-store. Разные единицы не суммируются.
Расчёт — бухгалтерское тождество, не причинная модель и не доказанная экономия.

## SQL и проверки

- [Мигрируемые views](../sql/views_v1.sql), [контракт сравнения](../sql/comparison_v1.sql).
- [Цена/количество и мост](../sql/decomposition_v1.sql).
- [Наблюдения, медианы и качество](../sql/dashboard_v1.sql).
- [SQL виртуальных datasets](../superset/bundle.py), [snapshot DDL](../superset/snapshot.sql).
- [Архитектура и ограничения](../DECOMPOSITION.md).

```bash
# Можно использовать отдельную БД audit в том же одноразовом контейнере.
docker exec tratte-public-demo createdb -U postgres audit
TRATTE_AUDIT_TEST_DSN='postgresql://postgres:synthetic-demo@localhost:55437/audit' \
  python -m pytest
ruff check .
```

`tests/test_public_demo.py`: стабильная fixture, маркировка,
реальный PostgreSQL → независимый oracle, чувствительность oracle к ошибке,
неполный июль и наличие публичных медиа. CI поднимает одноразовый PostgreSQL сам.
Другие SQL-тесты покрывают scope, timezone, stale mapping, NULL, порог двух чеков,
неизменяемые snapshot bindings и экспорт. Без DSN PostgreSQL-тесты явно skipped.

## Иллюстрации и видео

Три настоящих Superset-скриншота скопированы из проверенного synthetic #6,
без преобразования содержимого: bridge, detail, quality. Источник и проверка
[VALIDATION.md](../superset/VALIDATION.md). Скриншоты можно открыть отдельно,
чтобы прочитать большие таблицы на мобильном экране.

Видео `docs/assets/case/walkthrough.webm` — запись публичного demo 1280×900,
75 секунд, без звука, с русским VTT. Сценарий: 0–12с итог; 12–24с мост;
24–36с цена; 36–48с молоко; 48–60с рис; 60–70с охват; 70–75с неполный июль.
Запись не изображает работу Superset: это явно подписанная публичная иллюстрация.
Перед публикацией проверены кадры и fixture: только вымышленные строки,
без реальной почты, фискальных полей, истории покупок, токенов и рабочих DSN.
Публичное размещение и доставка сервиса проверяются отдельно: [VALIDATION.md](VALIDATION.md).

Для повторной записи тем же Playwright CLI (после запуска HTTP-сервера):

```bash
npx --yes --package @playwright/cli playwright-cli -s=demo open http://127.0.0.1:8767/demo.html
npx --yes --package @playwright/cli playwright-cli -s=demo resize 1280 900
npx --yes --package @playwright/cli playwright-cli -s=demo video-start /tmp/tratte-demo.webm --size=1280x900 --fps=15
npx --yes --package @playwright/cli playwright-cli -s=demo run-code --filename analytics/demo/record.js
npx --yes --package @playwright/cli playwright-cli -s=demo video-stop
```

Нужны browser/ffmpeg из той же версии Playwright, которую использует CLI.
При проверке использован CLI 0.1.22 и его установленный Chromium/ffmpeg runtime.
Видео публикуется только после просмотра кадров; автоматическая перезапись
опубликованного файла из команды записи не выполняется.
