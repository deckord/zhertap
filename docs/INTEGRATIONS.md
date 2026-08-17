# Интеграции и внешние источники

## Telegram

Используется aiogram 3.

Переменные:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_ADMIN_USER_IDS=
```

Бот работает в polling-режиме через контейнер `bot`.

Важно:

- Telegram token нельзя хранить в документации, Git и сообщениях.
- Бот рассчитан на private-чаты.
- Для админских кнопок используется `TELEGRAM_ADMIN_USER_IDS`.
- Для уведомлений администратору используется `TELEGRAM_ADMIN_CHAT_ID`.

## ApiPay / Kaspi

ApiPay нужен для автоматической оплаты через Kaspi.

Переменные:

```dotenv
APIPAY_ENABLED=true
APIPAY_API_KEY=
APIPAY_WEBHOOK_SECRET=
APIPAY_BASE_URL=https://api.apipay.kz/api/v1
APIPAY_TIMEOUT_SECONDS=30
APIPAY_POLLING_ENABLED=true
APIPAY_POLL_INTERVAL_SECONDS=20
APIPAY_POLL_ATTEMPTS=30
APP_BASE_URL=https://zhertap.kz
```

Webhook:

```text
POST https://zhertap.kz/webhooks/apipay
```

Что проверяет webhook:

- интеграция включена;
- есть webhook secret;
- корректная HMAC-подпись;
- валидный JSON;
- invoice относится к существующей заявке или доступу;
- сумма совпадает;
- статус `paid`, `cancelled`, `expired`, `error`.

Если webhook не приходит, включенный polling может периодически проверять invoice через ApiPay API.

Сценарии оплаты:

- Telegram-поиск создает invoice для конкретной `search_requests` заявки и после подтверждения доставляет полный отчет.
- Веб-кабинет создает account-level invoice на `/cabinet/payment`. Клиент с ПК видит QR Kaspi, с телефона может открыть платежную ссылку. После webhook/polling аккаунт получает `paid_access=true` и `access_expires_at` на 1 месяц; если Telegram привязан к этому аккаунту, единый платный доступ начинает действовать и в боте.
- Веб-страница оплаты не требует ручной проверки: `/cabinet/payment/status` опрашивает ApiPay, включает доступ при `paid`, а при `cancelled`/`expired`/`error` автоматически создает новый счет и фронт обновляет страницу. Для сканирования в Kaspi нужно показывать официальный `qr_image_url` из ответа ApiPay; самодельный QR из `qr_token_url` может не распознаваться Kaspi-приложением и используется только как fallback.
- Если клиент нажимает `Обновить QR`, backend сначала проверяет текущий invoice; если он уже `paid`, включает доступ, иначе отменяет старый active invoice и создает новый официальный QR. Это нужно для случаев, когда Kaspi не распознал QR или клиент отменил платеж.

## SMSC

SMSC используется для SMS-кода регистрации/подтверждения телефона в веб-кабинете. После регистрации клиент входит по телефону и паролю, чтобы не тратить SMS на каждый вход.

Переменные:

```dotenv
SMSC_ENABLED=true
SMSC_LOGIN=
SMSC_PASSWORD=
SMSC_BASE_URL=https://smsc.kz/sys/send.php
SMSC_SENDER=
SMSC_TIMEOUT_SECONDS=15
```

Что важно:

- логин/пароль SMSC не хранить в docs/Git;
- в development при `SMSC_ENABLED=false` код не отправляется наружу;
- в production ошибка SMSC должна показывать клиенту понятное сообщение;
- SMS-код хранится в БД только как hash;
- после 3 неверных попыток аккаунт временно блокируется на 5 минут.

## ЕГКН

Используется публичная кадастровая карта/ЕГКН:

```dotenv
EGKN_WFS_URL=https://map.gov4c.kz/geoserver/egkn/ows
EGKN_REST_URL=https://map.gov4c.kz/egkn/rest
EGKN_TIMEOUT_SECONDS=25
EGKN_REQUEST_ATTEMPTS=2
EGKN_VERIFY_TLS=false
```

Использование:

- справочники областей/районов/населенных пунктов;
- геометрия административных контуров;
- публичные кадастровые участки;
- соседний кадастровый номер.

Ограничение: публичный слой не доказывает юридическую свободу земли.

## OpenStreetMap / Overpass

Переменные:

```dotenv
ENABLE_LIVE_OSM=true
OVERPASS_URL=https://overpass-api.de/api/interpreter
OVERPASS_FALLBACK_URLS=https://overpass.private.coffee/api/interpreter
OSM_QUERY_TIMEOUT_SECONDS=25
OSM_TIME_BUDGET_SECONDS=120
OSM_BATCH_SIZE=8
OSM_ROAD_CLEARANCE_M=5
OSM_OPEN_WATER_CLEARANCE_M=30
```

Использование:

- дороги;
- здания;
- вода;
- кладбища;
- свалки;
- карьеры;
- охраняемые территории;
- часть инфраструктуры.

Ограничение: OSM может быть неполным или устаревшим.

## Генплан/ПДП

Система работает с официальными геопривязанными GeoJSON-слоями.

Переменные:

```dotenv
URBAN_PLAN_CHECK_MODE=strict
URBAN_PLAN_RED_LINE_BUFFER_M=5
URBAN_PLAN_MAX_UPLOAD_MB=20
URBAN_PLAN_SOURCE_DOMAINS=gov.kz,adilet.zan.kz,egov.kz,map.gov4c.kz,aisgzk.kz
```

Виды слоев:

- `allowed` - разрешающие зоны;
- `prohibited` - запретные зоны;
- `red_line` - красные линии.

PDF/JPG без геопривязки не является автоматическим источником. Для автоматической проверки нужен GeoJSON с доказанной привязкой и источником.

Покрытие по территориям хранится в `urban_plan_coverage`. Если для выбранной территории нет пригодного утвержденного цифрового слоя, поиск может автоматически продолжиться без генплана (`URBAN_PLAN_AUTO_WAIVE_UNAVAILABLE=true`) с явным предупреждением в отчете. Сломанные, спорные или запретные слои не обходятся автоматически.

Наполнение по регионам нужно вести через официальный источник -> геопривязанный слой -> проверка CRS/геометрии -> импорт в `urban_plan_layers` -> контрольная заявка. Приоритетные источники для поиска слоев: геопортал НИПД `map.gov.kz`, геопортал Госградкадастра `ggk.kz`, городские/областные геопорталы и нормативные документы на `adilet.zan.kz` как подтверждение статуса генплана.

Поддерживаемые автоматические источники-кандидаты:

- AIS GGK / State Urban Cadastre через `tools.genplan_ggk`;
- Smart GeoHub через `tools.smart_geohub_release`;
- Geonomix через `tools.genplan_geonomix_release`;
- generic WFS/GeoServer через `tools.genplan_wfs_release`.

Production на 17.08.2026 содержит 426 строк `urban_plan_layers`: 90 активных
`VERIFIED_STRICT/search` слоев для клиентского поиска в 30 областях применения
и 336 неактивных QA/shadow. Ручная очередь рассчитанных точек завершена:
641 из 641 проверена, `queued = 0`. Новые Smart GeoHub/Geonomix-слои включены только для
`ЛПХ:household` по узкой зоне `usl_i32=11010000` (`Территория усадебной
застройки`) и только после пересборки как `VERIFIED_STRICT/search`.
Павлодарская и частично Атырауская WFS-выгрузки,
AIS GGK и прочие shadow-слои остаются ручными/QA-источниками и не считаются
автоматической проверкой генплана до нового `VERIFIED_STRICT/search`-релиза.

Для территорий без пригодного цифрового слоя есть отдельный справочник ручной сверки `app/genplan_references.py`. Он используется в Telegram-отчетах, веб-кабинете и админской карточке заявки, чтобы дать клиенту кнопку на официальный документ/геопортал. Это fallback для визуальной проверки человеком, а не источник автоматического пространственного решения.

С 31.07.2026 справочник сначала ищет локальную библиотеку PDF/JPG/PNG/TIF
генпланов через `app/data/manual_genplans.json` и `/manual-genplans/...`.
Ссылки на `adilet.zan.kz` не должны быть основным клиентским действием, если
есть сама карта генплана/ПДП: Adilet подтверждает юридический акт, но обычному
клиенту для ручной сверки нужна карта.

Клиентские подписи статуса генплана/ПДП централизованы в
`app/urban_plan_labels.py`. Веб-кабинет, polling JSON и Telegram-отчеты должны
говорить клиенту не `waived/passed/blocked`, а понятный смысл: автоматическая
проверка выполнена, проверка не подключена и нужна ручная сверка, найдено
ограничение, либо проверка еще ожидается.

С 04.08.2026 поиск использует genplan-first prefilter там, где это возможно:
если в `urban_plan_layers` есть активный утвержденный `allowed`-полигон под
выбранное назначение и он пересекает выбранный район/населенный пункт, backend
сначала сужает область поиска до разрешенной зоны, а затем грузит ЕГКН.

Важная защита от ложных отказов: если слой подходит по metadata региона, но его
геометрия фактически не покрывает найденные кандидаты, такой слой не блокирует
заявку. Для этой конкретной заявки генплан считается непригодным/неподключенным,
и результат выдается как предварительный без автоматической проверки генплана
при включенном `URBAN_PLAN_AUTO_WAIVE_UNAVAILABLE=true`.

PDF/JPG/TIFF из локальной библиотеки генпланов можно показывать клиенту для
ручной сверки. Автоматической проверкой они становятся только после A1/A2
геопривязки, QA, экспорта/vectorize и импорта как `VERIFIED_STRICT/search`.

Последний подробный production-статус генпланов/ПДП: `docs/GENPLAN_STATUS_2026_08_17.md`.

## E-Qazyna

Переменные:

```dotenv
AUCTIONS_ENABLED=true
EQAZYNA_BASE_URL=https://sauda.e-qazyna.kz
EQAZYNA_SYNC_STATUSES=ApplicationsAccept,Pending,Running,SuccessProtocolSigned,FailureProtocolSigned,NullifyResultProtocolSigned,CancelBeforeStart
EQAZYNA_SYNC_INTERVAL_MINUTES=30
EQAZYNA_SYNC_MAX_PAGES=10
EQAZYNA_SYNC_MAX_LOTS=100
EQAZYNA_HISTORY_SYNC_STATUSES=SuccessProtocolSigned,FailureProtocolSigned,NullifyResultProtocolSigned,CancelBeforeStart
EQAZYNA_HISTORY_SYNC_MAX_PAGES=100
EQAZYNA_HISTORY_SYNC_MAX_LOTS=1000
EQAZYNA_HISTORY_SYNC_START_YEAR=2020
EQAZYNA_HISTORY_SYNC_WINDOW_DAYS=366
EQAZYNA_TIMEOUT_SECONDS=30
EQAZYNA_VERIFY_TLS=true
```

Что собирается:

- список земельных лотов;
- карточка лота;
- документы;
- статус торгов;
- цена;
- площадь;
- назначение;
- регион/район/населенный пункт;
- продавец;
- дата торгов;
- история изменений при повторной синхронизации.

Важно: текущий синхронизатор собирает активные/текущие статусы по обычному расписанию. Для исторической базы есть отдельный backfill E-Qazyna: он идет по архивным статусам и периодам публикации с `EQAZYNA_HISTORY_SYNC_START_YEAR` до текущей даты. Он не деактивирует активные лоты, не отправляет уведомления о новых торгах и не подает заявки; он только пополняет базу для истории, повторных размещений и аналитики цены.

## Внутренний API

Защищается `X-API-Key` через:

```dotenv
INTERNAL_API_KEY=
```

Основные endpoints:

- `GET /health`
- `POST /api/searches`
- `GET /api/searches/{request_id}`
- `GET /api/auctions`
- `GET /api/auctions/{lot_id}`
- `GET /api/auctions/stats/market`
- `GET /api/auctions/map/geojson`

В development без ключа возможны послабления в `app/security.py`, но production должен иметь непустой `INTERNAL_API_KEY`.

## Сайт и веб-кабинет `zhertap.kz`

Сайт сейчас реализован внутри того же FastAPI-приложения, а не как отдельная Tilda-страница. Nginx/SSL проксирует `https://zhertap.kz` на контейнер `web`.

Основные маршруты:

- `/` - landing.
- `/offer`, `/privacy`, `/terms` - юридические страницы.
- `/login` - вход/регистрация с переключателем режимов.
- `/register/request-code` - отправка SMS-кода регистрации через SMSC.
- `/register/verify` - проверка SMS-кода и установка пароля.
- `/logout` - выход.
- `/cabinet` - личный кабинет.
- `/cabinet/settings` - настройки и смена пароля.
- `/cabinet/telegram/link` - deep-link привязки Telegram.
- `/cabinet/search` - веб-поиск участков.
- `/cabinet/searches/{id}/status` - live-статус поиска без обновления страницы.
- `/cabinet/auctions` - аукционы.
- `/cabinet/auctions/favorites` - избранное.
- `/cabinet/auctions/compare` - сравнение.
- `/cabinet/auctions/subscriptions` - подписки.
- `/cabinet/feedback` - обратная связь.

Безопасная схема:

1. Frontend не хранит Telegram token, ApiPay key, SMSC password, database credentials.
2. Все платежи и webhook остаются на backend.
3. Веб-сессия хранится серверно, в браузере только `httpOnly` cookie.
4. Связь с Telegram идет через short-lived token, который потребляет бот.
5. Справочники для форм отдаются backend endpoints, а не свободным текстовым вводом.

Если в будущем снова появится Tilda или другой внешний frontend, он должен вызывать только отдельные безопасные backend endpoints через HTTPS и не получать внутренних секретов.
