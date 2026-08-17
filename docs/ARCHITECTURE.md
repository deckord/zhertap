# Архитектура Land Scout Kazakhstan

## Общая схема

```text
Telegram user
  -> app.bot / app.auction_bot
  -> PostgreSQL/PostGIS
  -> Celery worker
  -> ЕГКН / OSM / UrbanPlan layers / E-Qazyna / ApiPay

Web user
  -> zhertap.kz
  -> app.web / FastAPI
  -> PostgreSQL/PostGIS
  -> Celery worker
  -> SMSC / Telegram linking / ApiPay

Admin browser
  -> FastAPI web
  -> PostgreSQL/PostGIS

ApiPay
  -> POST /webhooks/apipay
  -> FastAPI
  -> PostgreSQL
  -> Telegram notification/delivery
```

## Runtime

Проект написан на Python 3.12.

Основные зависимости:

- FastAPI - веб-панель и API.
- aiogram 3 - Telegram-бот.
- Celery - фоновые задачи.
- Redis - брокер Celery.
- SQLAlchemy - ORM.
- PostgreSQL/PostGIS - production database.
- SQLite - локальная разработка по умолчанию.
- Shapely/pyproj - геометрия и преобразование координат.
- httpx - внешние HTTP-запросы.

## Слои приложения

### `app/main.py`

FastAPI-приложение:

- `/health`
- `/webhooks/apipay`
- `/api/searches`
- `/api/auctions`
- `/api/auctions/stats/market`
- `/api/auctions/map/geojson`
- `/api/auctions/{lot_id}`
- `/admin/*`

Также содержит админские страницы для заявок, аукционов, аналитики, генпланов и каталогов ЕГКН.

### `app/web.py`

Публичный сайт и личный кабинет `zhertap.kz`:

- `/` - landing;
- `/login` - вход по телефону и паролю;
- `/register/request-code` - отправка SMS-кода регистрации;
- `/register/verify` - проверка SMS-кода, установка пароля и старт trial;
- `/logout`;
- `/offer`, `/privacy`, `/terms`;
- `/cabinet` - обзор;
- `/cabinet/help` - клиентская инструкция простым языком;
- `/cabinet/onboarding/dismiss` - закрытие/завершение onboarding-тура;
- `/cabinet/settings` и `/cabinet/settings/password` - профиль и смена пароля;
- `/cabinet/telegram/link` - создание токена привязки Telegram;
- `/cabinet/search` и `/cabinet/searches/{id}` - веб-поиск и live-статус без обновления страницы;
- `/cabinet/catalog/*` - справочники ЕГКН для веб-форм;
- `/cabinet/auctions*` - лоты, справочники фильтров, избранное, сравнение, подписки, карточка;
- `/cabinet/feedback` - обратная связь клиента.

Логика сайта не дублирует Telegram-сценарии, а использует общие сервисы `create_search`, `dispatch_search`, `auction_service`, `feedback`.

### `app/bot.py`

Основной Telegram-бот для поиска мест под участки:

- `/start`
- выбор языка;
- принятие условий;
- выбор назначения;
- выбор области/района/населенного пункта;
- запуск поиска;
- проверка статуса;
- повтор ошибки;
- продолжение без генплана;
- оплата/обновление QR;
- админские callback-кнопки подтверждения/отклонения;
- команды `/terms`, `/privacy`, `/offer`, `/whoami`, `/status`.
- обратная связь через `/feedback` и callback `feedback:start`;
- привязка Telegram к веб-аккаунту через deep-link token.

Бот принимает только private-чаты. Групповые сообщения игнорируются отдельным group-router.

### `app/auction_bot.py`

Telegram-раздел аукционов:

- `/auctions`
- меню аукционов;
- фильтр по региону, назначению, цене и площади;
- просмотр лота;
- документы;
- история торгов;
- изменения;
- избранное;
- сравнение до 10 избранных лотов;
- подписки;
- оплата доступа.

### `app/tasks.py`

Celery tasks:

- `land_scout.process_search` - обработка заявки поиска места.
- `land_scout.reconcile_apipay_invoice` - проверка обычной оплаты.
- `land_scout.reconcile_auction_apipay_invoice` - проверка оплаты аукционного доступа.
- `land_scout.sync_auctions` - периодическая синхронизация E-Qazyna.
- `land_scout.recover_stale_searches` - каждые 5 минут восстанавливает зависшие обработки и готовые, но не доставленные уведомления.

При старте worker выполняется восстановление зависших заявок.
`land_scout.process_search` ретраит временные ошибки источников данных
(`EgknProviderError`, `OsmProviderError`, timeout) через стандартный Celery
retry. Только после исчерпания лимита заявка становится `failed`.

### `app/services.py`

Бизнес-логика поиска мест:

- создание заявок;
- очередь;
- обработка поиска;
- выдача бесплатного лимита;
- платный доступ;
- ApiPay webhook;
- доставка результатов;
- повтор ошибок;
- следующие 10 участков.
- строгая доставка готовых результатов: готовый ответ не считается уведомленным, пока отправка клиенту не прошла успешно;
- восстановление ready-delivery/paywall уведомлений через `ensure_ready_delivery`.

`ensure_ready_delivery` обязан повторно маршрутизировать Telegram-заявку, если
она находится в `ready`, имеет `telegram_chat_id`, одобренных кандидатов и ноль
доставленных кандидатов. Наличие `search_completed_notified_at` не должно
блокировать повторную доставку: это поле может быть заполнено после уведомления
о завершении поиска, даже если сам отчет/превью не дошел.
Для старого рассинхрона `free_preview_status=delivered` без `delivered_at` у
кандидатов восстановление временно возвращает превью в `pending` и отправляет
его заново через штатный `approve_free_preview`.

`retry_failed_search()` создает идемпотентную повторную заявку с теми же
параметрами и ссылкой `retry_of_request_id` на исходный сбой. Его используют
Telegram-кнопка `search:retry:{id}` и админская кнопка "Повторить поиск" на
странице сбойной заявки.

### `app/access.py`

Единая проверка доступа аккаунта:

- месячный paid access через `access_expires_at`;
- legacy paid access без `access_expires_at` остается активным;
- перенос Telegram-paid access в `accounts.paid_access`;
- 1 день полного trial после веб-регистрации;
- общий access kind для сайта, аукционов и обратной связи.

### `app/sms.py`

Интеграция SMSC для одноразовых кодов регистрации/подтверждения номера. Вход после регистрации идет по паролю, чтобы не тратить SMS на каждый логин.

### `app/feedback.py`

Обратная связь:

- сбор получателей Telegram-рассылок;
- отправка массового запроса впечатлений;
- хранение диалогов клиента;
- ответы админа в Telegram;
- платежный статус клиента в админке;
- сортировка новых клиентских сообщений вверх и выделение непрочитанных.

### `app/live_search.py`

Геометрический поиск по ЕГКН/OSM/генплану:

- загрузка границ района/населенного пункта;
- получение кадастровых участков;
- поиск свободных промежутков;
- размещение расчетного квадрата;
- исключение дорог/воды/объектов;
- проверка генплана/ПДП;
- расчет рейтинга и комментариев.

### `app/providers/egkn.py`

Интеграция с публичным ЕГКН:

- REST-каталог областей/районов/населенных пунктов;
- WFS слой зарегистрированных участков;
- WFS/REST таймауты и retry.

### `app/providers/osm.py`

Интеграция с Overpass/OpenStreetMap:

- дороги;
- здания;
- вода;
- кладбища;
- свалки;
- карьеры;
- охраняемые территории;
- инфраструктурные объекты.

### `app/providers/urban_plan.py`

Проверка официальных геопривязанных слоев генплана/ПДП:

- нормализация GeoJSON;
- допустимые зоны;
- запретные зоны;
- красные линии;
- районные и населенные слои.
- учет покрытия в `urban_plan_coverage`: `available`, `unavailable`, `broken`;
- автопродолжение без генплана при `unavailable`, если включен `URBAN_PLAN_AUTO_WAIVE_UNAVAILABLE=true`.

При повторном поиске по территории, где уже зафиксирован `unavailable`, система сначала смотрит `urban_plan_coverage` и не тратит время на повторную проверку генплана. При загрузке или включении слоя соответствующая запись покрытия удаляется, чтобы новый официальный слой сразу начал участвовать в проверке. Автопродолжение применяется только когда по выбранной области/району/населенному пункту/назначению нет пригодного утвержденного цифрового слоя. Если слой найден, но сломан, неполный или кандидат попал в запрет/красную линию, система не обходит проверку автоматически.

### `app/urban_plan_labels.py`

Единый слой клиентских текстов для статуса генплана/ПДП. Он не меняет
геометрию и решения поиска, а только переводит внутренние статусы в понятные
метки для веб-кабинета, polling JSON и Telegram:

- `passed` -> зеленая метка `Генплан/ПДП проверен автоматически`;
- `blocked` -> красная метка `Генплан/ПДП не подтвердил это место`;
- `unavailable`/`waived` -> желтая метка `Генплан/ПДП не подключен` и
  пояснение про ручную сверку;
- `pending`/пусто -> нейтральная метка `Генплан/ПДП ожидает проверки`.

В клиентском UI нельзя показывать сырой `waived`, `passed`, `blocked` или
`pending` без такого пояснения.

### `app/manual_genplans.py` и `app/genplan_references.py`

`app/genplan_references.py` выбирает ручной источник для клиента, если
автоматического strict-слоя нет или нужна дополнительная визуальная сверка.
Приоритет источников:

1. PDF/JPG/PNG/TIF из локальной библиотеки ручных генпланов, если файл реально
   доступен на диске.
2. Интерактивный официальный геопортал с картой/слоями.
3. Adilet или другой юридический документ только как fallback, когда карты
   генплана/ПДП нет.

Индекс файлов хранится в `app/data/manual_genplans.json`, генерируется
`tools/build_manual_genplan_manifest.py`. Файлы отдаются через
`/manual-genplans/{asset_id}/{filename}`. Корень файлов задается
`MANUAL_GENPLAN_FILES_ROOT`; если он не задан, dev/prod пробуют стандартные
пути, включая `/opt/land-scout/manual-genplans/extracted`.

Production-снимок на 17.08.2026: 426 строк `urban_plan_layers`, из них 90
активных `VERIFIED_STRICT/search` в 30 территориально-целевых областях
применения и 336 неактивных QA/shadow. Ручная очередь рассчитанных точек
закрыта полностью: 641 из 641 проверена, `queued = 0`. Неактивные слои и
неразобранные легенды — это backlog расширения покрытия, а не остаток этой
ручной проверки. Shadow-слои нельзя включать ручным изменением флагов БД;
нужен новый проверенный `VERIFIED_STRICT/search` release.

### `app/auction_service.py`

Бизнес-логика аукционов:

- upsert лота;
- документы;
- история изменений;
- история публикаций;
- фильтры;
- рейтинг;
- статистика;
- подписки;
- уведомления;
- GeoJSON карты.

### `app/providers/eqazyna.py`

Сборщик E-Qazyna:

- парсинг списка лотов;
- парсинг карточки лота;
- извлечение документов;
- статусы поиска;
- постраничный crawl.

### `app/auction_geo.py`

Геоаналитика аукционного лота:

- извлечение координат из JSON/текста;
- расчет расстояний по reference objects;
- fallback-статусы `no_coordinates` и `no_reference_objects`.

### `app/auction_access.py`

Платный доступ к аукционам:

- бесплатный первый лот;
- старт оплаты;
- обновление QR;
- webhook/polling;
- активация доступа на 1 месяц.

### `app/apipay.py`

Клиент ApiPay:

- создание invoice;
- получение invoice;
- отмена invoice;
- проверка webhook подписи.

### `app/funnel.py`

Тексты и логика клиентской воронки v2:

- welcome;
- legal links;
- payment offer;
- progress;
- completed messages.

### `app/models.py`

Основные таблицы:

- `search_requests`
- `candidates`
- `funnel_events`
- `accounts`
- `web_login_codes`
- `web_sessions`
- `telegram_link_tokens`
- `feedback_conversations`
- `feedback_messages`
- `feedback_broadcasts`
- `feedback_broadcast_recipients`
- `urban_plan_layers`
- `urban_plan_coverage`
- `auction_lots`
- `auction_documents`
- `auction_lot_history`
- `auction_lot_changes`
- `auction_favorites`
- `auction_subscriptions`
- `auction_notifications`
- `auction_access`

### `app/db.py`

Инициализация базы и ручные миграции. Alembic пока не используется. При старте `web`, `bot`, `worker` вызывается `init_db()`.

## Данные и статусы

### SearchRequest

Ключевые статусы:

- `queued`
- `processing`
- `review`
- `ready`
- `delivered`
- `failed`

Внутренний `SearchRequest.status` не выводится администратору как единственный
смысловой статус. В админке используется `admin_search_status()` из `app/main.py`,
который учитывает `status`, `search_outcome`, оплату, доставку и количество
одобренных кандидатов:

- `Участки не найдены` - поиск завершен, подходящих вариантов нет; часто это
  внутренний `ready` + `search_outcome=no_candidates`.
- `Найдено, ждёт отправки` - кандидаты есть, но отчет еще не доставлен или
  нужен следующий операторский/платежный шаг.
- `Ждёт оплаты` - кандидаты есть и выставлен счет.
- `Оплачено, не отправлено` - оплата подтверждена, но доставку нужно проверить.
- `Отчёт отправлен` - клиенту доставлены найденные варианты.
- `Сбой поиска` - технический сбой (`failed`).

Поэтому `ready` сам по себе означает только "поиск закончен и заявка готова к
следующему шагу"; он не гарантирует, что участки найдены или отчет отправлен.

Страница `/admin/requests/{id}` тоже не должна разговаривать с оператором
сырыми статусами. Для кандидатов используются:

- `review_status_labels` - перевод `pending/approved/rejected` в простые
  русские статусы;
- `urban_plan_badge_payload()` - человеческая метка генплана/ПДП;
- `admin_candidate_note()` - короткое объяснение "что это значит" для
  оператора.

Длинные `risk_notes`, `review_status` и `urban_plan_status` остаются только в
раскрываемом техническом блоке. Для заявок без `telegram_chat_id` ручная кнопка
"Сформировать результат" не показывается как действие, потому что адресата для
отправки нет.

Ключевые платежные статусы:

- `not_requested`
- `awaiting_transfer`
- `pending_confirmation`
- `paid`
- `rejected`

Ключевые статусы генплана:

- `pending`
- `passed`
- `unavailable`
- `blocked`
- `waived`

`waived` бывает ручным (`manual`) и автоматическим (`auto_no_approved_layer`). Автоматический `waived` означает не согласие пользователя, а системное решение: в БД нет пригодного цифрового слоя для выбранной территории, поэтому анализ продолжается по ЕГКН/OSM с предупреждением в отчете.

Для отображения клиенту использовать `urban_plan_badge_payload()` и
`telegram_urban_plan_line()` из `app/urban_plan_labels.py`.

### Candidate

Кандидат - расчетное место, а не официальный свободный участок. Важные поля:

- координаты;
- соседний кадастровый номер;
- расстояние до ориентира;
- purpose соседнего участка;
- OSM/генплан статусы;
- review_status;
- source_checked_at.

### AuctionLot

Карточка лота E-Qazyna:

- source_lot_id;
- source_url;
- status;
- region/district/locality;
- cadastre_number;
- area_ha;
- land_rights;
- functional purpose levels;
- start_price/guarantee/sale_price;
- auction_starts_at;
- seller;
- raw_payload_json.

## Платный доступ

Сейчас используется единый доступ:

- цена задается `PLATFORM_ACCESS_PRICE_KZT` и/или `AUCTION_ACCESS_PRICE_KZT`;
- новый доступ действует 1 месяц на Telegram user ID и/или связанный web account;
- ранее оплаченные пользователи без даты окончания не теряют legacy-доступ;
- применяется к поиску участков и аукционам;
- бесплатный/истекший режим скрывает точные координаты, карту, ЕГКН, документы и платные действия;
- бесплатный лимит аукционов - `AUCTION_FREE_PREVIEW_LOTS`.
- trial после веб-регистрации задается `TRIAL_ACCESS_ENABLED` и `TRIAL_ACCESS_DAYS` и дает полный доступ до окончания срока.

## Поисковый pipeline и очереди

Клиентский поиск земли, платежные reconciliation-задачи и recovery идут через
Celery queue `critical`. Синхронизация аукционов E-Qazyna вынесена в отдельную
queue `auctions` и отдельный контейнер `auction_worker`, чтобы долгий crawl не
задерживал выдачу результатов клиентам.

Live search работает в таком порядке:

1. Выбранная область/район/населенный пункт превращаются в геометрию поиска.
2. Если есть активные утвержденные `allowed`-полигоны генплана/ПДП под выбранное
   назначение, область поиска сужается до пересечения с ними.
3. Загружаются участки ЕГКН внутри итоговой области.
4. Строятся возможные промежутки между зарегистрированными участками.
5. OSM отсекает дороги, объекты, воду, кладбища и другие явные пересечения,
   если Overpass доступен.
6. `evaluate_urban_plan()` проверяет кандидатов по `allowed`, `prohibited` и
   `red_line` слоям.
7. Если пригодного слоя для конкретной заявки нет, при
   `URBAN_PLAN_AUTO_WAIVE_UNAVAILABLE=true` результат выдается как
   предварительный без ручного ожидания администратора.

Подробный статус production-слоев генплана/ПДП: `docs/GENPLAN_STATUS_2026_08_17.md`.

## Веб-идентичность

Центральная таблица веб-идентичности - `accounts`.

- `phone` - основной login identifier веба.
- `password_hash` и `password_set_at` - вход по паролю.
- `telegram_user_id`, `telegram_chat_id`, `telegram_linked_at` - связь с Telegram.
- `paid_access`, `access_granted_at`, `access_expires_at` - оплаченный доступ; `access_expires_at=NULL` у старых paid-записей означает legacy-доступ без ограничения срока.
- `trial_started_at`, `trial_expires_at` - одноразовый тестовый доступ.
- `offer_version`, `offer_accepted_at`, `offer_accepted_ip`, `offer_accepted_user_agent` - юридическое согласие.
- `onboarding_tour_available_at`, `onboarding_tour_dismissed_at` - показ и закрытие onboarding-тура для новых регистраций.
- `failed_login_attempts`, `locked_until` - защита от перебора.

## Аналитика

События пишутся в `funnel_events`.

Админ исключается через:

```dotenv
ANALYTICS_EXCLUDED_TELEGRAM_USER_IDS=70557953
```

Админская аналитика доступна в `/admin/analytics`.

Панель аналитики намеренно смешивает два источника:

- пользовательские шаги воронки считаются по `funnel_events` и уникальным
  `funnel_session_id`;
- фактические счета, оплаты и доставленные платные отчеты по Telegram-поиску
  считаются по `search_requests`, а не только по событиям `invoice_created`,
  `payment_paid`, `report_delivered`.

Это нужно потому, что ApiPay может подтвердить оплату уже доставленной заявки:
в таком случае `search_requests.payment_status='paid'` является источником
истины, даже если событие `payment_paid` не было создано повторной доставкой.

Блок `Сайт` в аналитике считает только веб-аккаунты и `account_payments`.
Бесплатные админские доступы с `payment_amount_kzt=0` не считаются как
реальные оплаты. Telegram-оплаты конкретных поисковых заявок отображаются в
общей воронке поиска земли.

## Что пока не является полным MVP аукционов

Реализовано много функций мониторинга. Для E-Qazyna добавлен отдельный backfill-процесс архива: он идет по архивным статусам и периодам публикации, пишет отдельный источник `eqazyna_history_backfill`, не деактивирует активные лоты и не отправляет пользовательские уведомления. Текущая система также продолжает накапливать историю с момента мониторинга и хранит изменения найденных лотов.

Экспорт PDF/Excel по текущему решению не развивался в рамках последних задач.
