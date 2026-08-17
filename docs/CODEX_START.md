# Стартовый контекст для нового чата Codex

Скопируйте этот файл или просто скажите новому чату Codex:

> Работай по проекту Land Scout Kazakhstan. Сначала прочитай `docs/PROJECT_HANDOVER.md`, `docs/MASTER_SPEC_STATUS.md`, `docs/ARCHITECTURE.md`, `docs/WEB_CABINET_ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/INTEGRATIONS.md` и только потом вноси изменения.

## Рабочая папка

```text
C:\Users\medadmin\Documents\Codex\2026-06-30\vj\land-scout-bot
```

## Сервер

```text
Production domain: https://zhertap.kz
Production public IP: <production-host>
Production host label: <production-hostname>
Production SSH user: medadmin
/opt/land-scout/land-scout-bot
```

Старый адрес `<old-private-host>` и любые `172.*` private/local IP не использовать как цель деплоя. Боевой деплой, `scp`/`pscp`, `ssh`/`plink` и проверки production выполнять только против `<production-host>` или домена `zhertap.kz`.

## Главное правило

Не начинать с переписывания архитектуры. Проект уже работает в production. Любое изменение делать маленьким, проверяемым патчем:

1. Прочитать соответствующие модули.
2. Внести локальные изменения.
3. Запустить `ruff`.
4. Запустить релевантные тесты.
5. Если менялась общая логика, запустить полный `pytest`.
6. Залить на сервер.
7. Пересобрать контейнеры.
8. Проверить `docker compose ps` и логи.

Если менялись шаблоны/CSS сайта или админки, поднять версию query string у подключенного CSS/JS, чтобы браузер не держал старый кэш.

## Быстрые команды локально

```powershell
cd C:\Users\medadmin\Documents\Codex\2026-06-30\vj\land-scout-bot
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest
```

## Быстрые команды на сервере

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose ps
sudo docker compose logs --tail=150 web worker bot
sudo docker compose up -d --build web worker bot
```

## Что нельзя делать без отдельного согласования

- Удалять или сбрасывать production БД.
- Менять цену, лимиты и юридические тексты без явного запроса.
- Публиковать токены, API keys, пароли.
- Отключать юридические предупреждения.
- Выдавать расчетные места как официально свободные участки.
- Использовать PDF/JPG генплан как доказанный слой без геопривязки и проверки.
- Делать массовую деактивацию аукционных лотов после неполного crawl.

## Основные зоны кода

- `app/bot.py` - клиентский Telegram-сценарий поиска земли.
- `app/auction_bot.py` - Telegram-сценарий аукционов.
- `app/main.py` - FastAPI, админка, API, webhook.
- `app/services.py` - бизнес-логика поиска земли и оплаты.
- `app/live_search.py` - геометрический поиск мест.
- `app/auction_service.py` - бизнес-логика аукционов.
- `app/providers/egkn.py` - ЕГКН.
- `app/providers/osm.py` - OSM/Overpass.
- `app/providers/urban_plan.py` - генплан/ПДП.
- `app/providers/eqazyna.py` - E-Qazyna.
- `app/apipay.py` - ApiPay.
- `app/models.py` - модели БД.
- `app/db.py` - init и ручные миграции.

## Общее ТЗ и статусы

Перед планированием новых функций смотри `docs/MASTER_SPEC_STATUS.md`. Там зафиксировано, что уже выполнено, что частично реализовано, что отложено и что еще не сделано.

## Текущее состояние продукта

Рабочие направления:

- публичный сайт `zhertap.kz`;
- веб-регистрация по телефону через SMSC и вход по паролю;
- личный кабинет клиента с единым аккаунтом для веба и Telegram;
- 1 день полного тестового доступа после регистрации;
- `/cabinet/help` с клиентской инструкцией простым языком;
- необязательный onboarding-тур только для новых веб-регистраций; закрытие хранится в `accounts.onboarding_tour_dismissed_at`;
- поиск расчетных мест под ЛПХ/садоводство;
- бесплатный/истекший режим показывает результаты с ограничениями;
- единый платный доступ 1990 KZT/месяц; срок хранится отдельно для каждого клиента;
- legacy-клиенты, у которых оплаченный доступ был выдан без даты окончания, остаются активными;
- ApiPay/Kaspi;
- генплан/ПДП по загруженным официальным GeoJSON;
- клиентские статусы генплана/ПДП в веб-кабинете и Telegram выводятся через
  `app/urban_plan_labels.py`: зеленый `проверяется автоматически`, желтый
  `нужна ручная сверка`, красный `есть ограничение`, нейтральный
  `проверка ожидается`. В UI нельзя показывать сырой `waived/passed/blocked`
  без понятного пояснения для клиента;
- production active genplan coverage after 2026-07-31 imports: 24 active groups / 72 active `VERIFIED_STRICT/search` layer rows. Region-wide: Акмолинская область, Алматинская область, Жетісу, Западно-Казахстанская область, Карагандинская область, Костанайская область, Кызылординская область, Мангистауская область, Туркестанская область, Улытауская область. City/scope-specific: Астана, Шымкент, Актобе, Атырау, Тараз, Павлодар, Петропавловск, Шахтинск, Аркалык, Костанай, Лисаковск, Рудный, Тобыл, Житикара;
- production genplan totals after 2026-07-31 QA import: 378 `urban_plan_layers`, 72 active `VERIFIED_STRICT/search` rows, 306 inactive `WARNING/shadow` rows;
- current production genplan status on 2026-08-17: all 641 manual candidate points reviewed (`queued=0`); 426 `urban_plan_layers`, including 90 active `VERIFIED_STRICT/search` rows in 30 scopes and 336 inactive QA/shadow rows. Active rows: `LPH` 3, `LPH:household` 69, `Gardening` 18. The inactive/source/legend backlog is future coverage work, not unfinished manual point review. Canonical status: `docs/GENPLAN_STATUS_2026_08_17.md`;
- production code on 2026-08-04 was checked against local SHA-256 for `app/providers/urban_plan.py`, `app/live_search.py`, `app/services.py`, `app/schemas.py`, `docker-compose.yml`; hashes matched. Production queues were empty: `critical=0`, `auctions=0`;
- genplan-first search is active: when approved allowed polygons overlap the selected area, the live search restricts EGKN loading to those polygons first. If a broad metadata layer does not spatially cover the selected candidate points, it is treated as unavailable for that request instead of blocking the whole region;
- remaining shadow genplan candidates from AIS GGK, Smart GeoHub, WFS/GeoServer and Geonomix must not be enabled without a new reviewed `VERIFIED_STRICT/search` release;
- local manual genplan bbox audit from 2026-08-03: 175 of 175 current workbench records resolve to a map bbox (`85 EGKN`, `90 static city/district fallback`, `0 Nominatim`). The wrong ZKO copy of Aktau is excluded from automatic processing, and the Mangystau Aktau copy is the canonical manual record. See `C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4\records.csv` and `C:\Users\medadmin\Documents\Codex\genplan\work\operator-queue.csv`;
- local workbench note: `tools.genplan_workbench` now uses the same EGKN/static/Nominatim bbox resolver chain and `workbench-manifest.json` includes `bbox_status`, `bbox_source`, `bbox_label`, `bbox_reason` when rebuilt with `--bbox-audit-records`. The workbench also exposes `Autoreg priority` filtering/sorting from conservative diagnostics, but this is only an operator aid, not approval;
- local manual genplan readiness from 2026-08-03: `C:\Users\medadmin\Documents\Codex\genplan\work\readiness-v7` has 175 workbench records after selected/split PDF page rendering: 175 need A1 control points, 0 bbox conflicts, and 0 remaining `page_selection` tasks. `records.csv` includes direct local workbench URLs;
- local manual genplan conservative autoreg from 2026-08-03: `C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v1` processed all 175 current workbench records. It produced diagnostics/proposed matching artifacts only; `registration_counts.needs_manual=175`, `qa_or_strict_automatic=false`, so no raster/PDF genplan is approved for automatic customer checking without A1/A2 QA and import.
- local autoreg diagnostics from 2026-08-04: `C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v1` contains `attempts.csv`, `reason-counts.csv`, `summary.json`, and `operator-priority.csv`. Current summary: 175 assets, 350 attempts, 0 pipeline-error assets, 0 safe proposed GCP attempts. Use `operator-priority.csv` to process the most promising manual genplans first in `http://127.0.0.1:8765`.
- local manual genplan conservative autoreg v2 from 2026-08-04: `C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v2` completed all 175 current workbench records with 0 failed assets and 0 pipeline errors. Diagnostics are in `C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v2`: 350 attempts, 0 safe proposed GCP attempts, and 12 operator-only diagnostic anchor attempts. These anchors are only visual hints for manual A1 georeferencing; they must not be imported as approved GCPs or used in customer checks.
- local workbench manifest was rebuilt against `workbench-autoreg-v2` and local `tools.genplan_workbench` was restarted on `http://127.0.0.1:8765`. `C:\Users\medadmin\Documents\Codex\genplan\work\readiness-v8` reports 175 records, 175 resolved bboxes, and 175 `gcp_needed` tasks. Operator HTML packs for the top 50 records are in `C:\Users\medadmin\Documents\Codex\genplan\work\operator-packs-v2`.
- local A1 acceleration from 2026-08-04: the workbench has a `Diagnostic anchors` filter and `Load N anchors` button. Use it only to create draft GCP rows for operator verification; it does not approve a raster/PDF genplan for customer search by itself.
- local A1 seed from 2026-08-04: `tools.genplan_seed_diagnostic_gcps` created 12 draft workbench `gcps.json` files from diagnostic anchors in `C:\Users\medadmin\Documents\Codex\genplan\workbench_data`; a repeat run skipped all 12 existing drafts. The drafts remain `proposed` and require visual A1/A2 QA before import.
- аукционы E-Qazyna;
- первый аукционный лот бесплатно;
- избранное, сравнение, подписки;
- аналитика по аукционам;
- обратная связь в Telegram, веб-кабинете и админке с диалогами клиентов.

Отдельные будущие задачи:

- полная ретроспективная загрузка истории E-Qazyna;
- QA и продвижение оставшихся shadow-генпланов в `VERIFIED_STRICT/search`;
- отдельные адаптеры/ручная геопривязка для Алматы city, СКО, ВКО, Абай и оставшихся порталов;
- автоматическая карта для фронтенда;
- улучшение качества геоаналитики аукционов при наличии координат и reference objects.
