# Эксплуатация, деплой и диагностика

## Сервер

```text
Domain: https://zhertap.kz
Public IP: <production-host>
Host label: <production-hostname>
SSH user: medadmin
Project: /opt/land-scout/land-scout-bot
Runtime: Docker Compose
```

Критично: `<old-private-host>`, `<old-private-host>` и любые другие `172.*` адреса не использовать как цель боевого деплоя. Это старый/локальный/private контур. Production-копирование и production-команды выполнять только на `<production-host>`.

## Проверка состояния

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose ps
sudo docker compose logs --tail=150 web worker bot
```

## Перезапуск

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose up -d --build web worker bot
```

Если менялись периодические задачи:

```bash
sudo docker compose up -d --build web worker bot beat
```

Если менялись только сайт, админские шаблоны, CSS/JS или FastAPI routes:

```bash
sudo docker compose up -d --build web
```

Если менялись `app/services.py`, `app/tasks.py`, поиск, доставка результатов, ApiPay polling/recovery:

```bash
sudo docker compose up -d --build web worker beat
```

Если менялись Telegram handlers:

```bash
sudo docker compose up -d --build bot
```

## Применение схемы БД

Миграции пока ручные внутри `app/db.py`, вызываются через `init_db()`.

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose run --rm -T --no-deps web python -c "from app.db import init_db; init_db(); print('DB schema updated')"
```

## Проверка, что приложение импортируется

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose run --rm -T --no-deps web python -c "from app.main import app; from app.auction_service import sync_current_auctions; print('ok')"
```

## Ручной запуск синхронизации E-Qazyna

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose run --rm -T web python -c "from app.db import SessionLocal; from app.auction_service import sync_current_auctions; s=SessionLocal(); print(sync_current_auctions(s).__dict__); s.close()"
```

Если удобнее читать многострочную версию:

```bash
sudo docker compose run --rm -T web python - <<'PY'
from app.db import SessionLocal
from app.auction_service import sync_current_auctions
s = SessionLocal()
try:
    result = sync_current_auctions(s)
    print(result.__dict__)
finally:
    s.close()
PY
```

## Ручная синхронизация реестра генпланов ГГК

Каталог источников сохраняется в `urban_plan_sources`. Это не включает слой в клиентский поиск: для этого нужен build, import и QA.

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose run --rm -T web python - <<'PY'
from app.db import SessionLocal, init_db
from app.genplan_sources import sync_ggk_urban_plan_sources

init_db()
s = SessionLocal()
try:
    print(sync_ggk_urban_plan_sources(s))
finally:
    s.close()
PY
```

Для региональных Smart GeoHub-порталов:

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose run --rm -T web python - <<'PY'
from app.db import SessionLocal, init_db
from app.genplan_sources import sync_smart_geohub_urban_plan_sources

init_db()
s = SessionLocal()
try:
    print(sync_smart_geohub_urban_plan_sources(s))
finally:
    s.close()
PY
```

Проверить, какие Smart GeoHub-источники реально отдают объекты и геометрию:

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose exec -T web python - <<'PY'
from app.db import SessionLocal
from app.genplan_sources import probe_smart_geohub_urban_plan_sources

with SessionLocal() as s:
    print(probe_smart_geohub_urban_plan_sources(s, limit=60))
PY
```

Статусы после проверки:

- `catalog_found` - слой найден в каталоге, геометрия еще не проверялась.
- `geometry_found` - `/api/list` и `/api/geometry` вернули sample-геометрию; можно готовить импорт-кандидат.
- `no_features` - коллекция есть, но sample-объектов нет.

Важно: `geometry_found` не является разрешением использовать слой в клиентском поиске. Нужны экспорт полной геометрии, маппинг зон, независимое QA и импорт в `urban_plan_layers`.

Быстро посчитать объекты в Smart GeoHub-слое или конкретном коде без выгрузки
геометрии:

```bash
cd /opt/land-scout/land-scout-bot
sudo mkdir -p /opt/land-scout/genplan/smart-geohub/counts
sudo docker compose run --rm -T \
  -v /opt/land-scout/genplan:/exports \
  web python -m tools.smart_geohub_export \
  --base-url https://map.iaqmola.kz/ \
  --collection gpzone-jil \
  --search-field usl_i32 \
  --search-text 11010000 \
  --output-dir /exports/smart-geohub/counts/akmola-lph \
  --count-only
```

Экспортировать Smart GeoHub-коллекцию в GeoJSON-кандидат для QA:

```bash
cd /opt/land-scout/land-scout-bot
sudo mkdir -p /opt/land-scout/genplan/smart-geohub
sudo docker compose run --rm -T \
  -v /opt/land-scout/genplan:/exports \
  web python -m tools.smart_geohub_export \
  --base-url https://map.iaqmola.kz/ \
  --collection gpzone-jil \
  --search-field usl_i32 \
  --search-text 11010000 \
  --output-dir /exports/smart-geohub/akmola-gpzone-jil \
  --max-features 10000
```

Для короткого пилота можно поставить `--max-features 90`. В этом случае manifest будет содержать `truncated_by_limit=true`; такой файл нельзя считать полным слоем.

Собрать полный Smart GeoHub release-кандидат для импорта в `urban_plan_layers`:

```bash
cd /opt/land-scout/land-scout-bot
sudo mkdir -p /opt/land-scout/genplan/smart-geohub/releases
sudo docker compose run --rm -T \
  -v /opt/land-scout/genplan:/exports \
  web python -m tools.smart_geohub_release \
  --base-url https://map.almobl.kz/ \
  --output-dir /exports/smart-geohub/releases/almaty-region-lph-household-v1 \
  --release-id almaty-region-smart-geohub-lph-household-v1 \
  --region "Алматинская область" \
  --district "*" \
  --locality "*" \
  --purpose "ЛПХ:household" \
  --title "Smart GeoHub Алматинской области: функциональные зоны и красные линии" \
  --approval-document "Официальные слои регионального геопортала Smart GeoHub Алматинской области" \
  --source-authority "Геопортал Алматинской области" \
  --source-url https://map.almobl.kz/ \
  --allowed gpzone-jil_noedit \
  --allowed-search usl_i32=11010000 \
  --prohibited gpzone-restrict_noedit \
  --prohibited gpzone-san_noedit \
  --prohibited gpzone-protect_noedit \
  --prohibited gpzone-transport_noedit \
  --red-line gpreg-redline_noedit \
  --red-line pdpreg-redline_noedit \
  --geometry-workers 8
```

Затем обязательно:

```bash
sudo docker compose run --rm -T \
  -v /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_import \
  --manifest /exports/smart-geohub/releases/almaty-region-lph-household-v1/release-manifest.json \
  --dry-run

sudo docker compose run --rm -T \
  -v /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_import \
  --manifest /exports/smart-geohub/releases/almaty-region-lph-household-v1/release-manifest.json
```

На 31.07.2026 в production есть 378 строк `urban_plan_layers`: 72 активных
`VERIFIED_STRICT/search` и 306 неактивных `WARNING/shadow`. Автопроверка
генплана активна по 24 группам: регионально для Акмолинской, Алматинской,
Жетісу, ЗКО, Карагандинской, Костанайской, Кызылординской, Мангистауской,
Туркестанской и Улытауской областей; точечно для Астаны, Шымкента, Актобе,
Атырау, Тараза, Павлодара, Петропавловска, Шахтинска, Аркалыка, Костаная,
Лисаковска, Рудного, Тобыла и Житикары. Новые региональные релизы включены только для
`ЛПХ:household` через узкие Smart GeoHub/Geonomix-слои по `usl_i32=11010000`.

Критично: оставшиеся shadow-слои нельзя включать в автоматический поиск
ручным изменением флагов в БД. Для продвижения в боевой режим нужен новый
release с `VERIFIED_STRICT`, `release_mode=search`, стабильным
`layer_sha256`/`release_policy` в `source-manifest.json` и независимой приемкой.

### Generic WFS / GeoServer genplan release

Для официальных GeoServer/WFS-порталов использовать
`tools.genplan_wfs_release`. Пример частичного shadow-релиза Атырау:

```bash
cd /opt/land-scout/land-scout-bot
mkdir -p /opt/land-scout/genplan/wfs/releases

docker compose run --rm -T \
  --volume /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_wfs_release \
  --base-url https://eatyrau.kz/geoserver/gis_atyrau/wfs \
  --output-dir /exports/wfs/releases/atyrau-wfs-lph-household-v1 \
  --release-id atyrau-wfs-lph-household-v1 \
  --profile lph-household \
  --region "Атырауская область" \
  --district "*" \
  --locality "*" \
  --title "Геопортал Атырауской области: частичные WFS-слои генплана, ПДП и красных линий" \
  --approval-document "Официальный геопортал Атырауской области; WFS GeoServer gis_atyrau" \
  --source-authority "Геопортал Атырауской области" \
  --source-url https://eatyrau.kz/map/ \
  --allowed gis_atyrau:gp_kg_batyrbek_usadebnayaZASTROYKA \
  --allowed gis_atyrau:gp_kg_g_alipov_zhilaya_zastr \
  --prohibited gis_atyrau:waterprotect_zone \
  --prohibited gis_atyrau:g_cemetery \
  --prohibited gis_atyrau:san_zone \
  --red-line gis_atyrau:arch_redlines \
  --red-line gis_atyrau:g_redlinesunited
```

После сборки:

```bash
docker compose run --rm -T \
  --volume /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_import \
  --manifest /exports/wfs/releases/atyrau-wfs-lph-household-v1/release-manifest.json \
  --dry-run < /dev/null

docker compose run --rm -T \
  --volume /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_import \
  --manifest /exports/wfs/releases/atyrau-wfs-lph-household-v1/release-manifest.json \
  < /dev/null
```

Важно для remote `base64 | bash` скриптов: добавлять `< /dev/null` к
`docker compose run`, иначе compose может прочитать остаток bash-скрипта из
stdin, и следующие команды не выполнятся.

### Geonomix genplan release

Для Geonomix-порталов использовать `tools.genplan_geonomix_release`.
Боевой strict-релиз должен явно задавать QA и роли:
Обычный случай:

```bash
docker compose run --rm -T \
  --volume /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_geonomix_release \
  --base-url https://map.e-zhetisu.kz \
  --output-dir /exports/geonomix/releases/zhetisu-lph-household-v1 \
  --release-id zhetisu-geonomix-lph-household-v1 \
  --profile lph-household \
  --region "Жетісу облысы" \
  --district "*" \
  --locality "*" \
  --title "Геопортал области Жетісу: функциональные зоны и красные линии" \
  --approval-document "Официальные слои регионального геопортала области Жетісу" \
  --source-authority "Геопортал области Жетісу" \
  --source-url https://map.e-zhetisu.kz \
  --allowed gpzone-jil_noedit \
  --allowed-search usl_i32=11010000 \
  --prohibited gpzone-restrict_noedit \
  --prohibited gpzone-san_noedit \
  --prohibited gpzone-rec_noedit \
  --prohibited gpzone-transport_noedit \
  --red-line gpreg-redline_noedit \
  --red-line pdpreg-redline_noedit \
  --qa-status VERIFIED_STRICT \
  --release-mode search \
  --reviewed-at-utc 2026-07-31T00:00:00Z \
  --operator codex-geonomix-builder \
  --reviewer codex-geonomix-reviewer \
  --geometry-workers 8 < /dev/null
```

Для Туркестанской области зоны лежат в одной коллекции; разрешающая зона
фильтруется по `usl_i32=11010000`, а запретные берутся как не начинающиеся на
`110`:

```bash
docker compose run --rm -T \
  --volume /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_geonomix_release \
  --base-url https://map.iturkistan.kz \
  --output-dir /exports/geonomix/releases/turkistan-lph-household-v1 \
  --release-id turkistan-geonomix-lph-household-v1 \
  --profile lph-household \
  --region "Туркестанская область" \
  --district "*" \
  --locality "*" \
  --title "Геопортал Туркестанской области: функциональные зоны и красные линии" \
  --approval-document "Официальные слои регионального геопортала Туркестанской области" \
  --source-authority "Геопортал Туркестанской области" \
  --source-url https://map.iturkistan.kz \
  --allowed gpzone-noedit \
  --allowed-list-search usl_i32=11010000 \
  --allowed-prefix usl_i32=11010000 \
  --prohibited gpzone-noedit \
  --prohibited-not-prefix usl_i32=110 \
  --red-line gpreg-redline_noedit \
  --geometry-workers 8 < /dev/null
```

### AIS GGK shadow batch

Use this when a GGK document should be stored as an inactive candidate first.
Shadow imports are safe for production because `approved_for_search=false`.

```bash
mkdir -p /opt/land-scout/genplan/ggk-shadow-batch-YYYYMMDD

docker compose run --rm -T \
  -v /opt/land-scout/genplan:/exports \
  web python -m tools.genplan_ggk_shadow_batch \
  --profile lph-household \
  --output-dir /exports/ggk-shadow-batch-YYYYMMDD/lph-household \
  --skip-document-id 3607 \
  --skip-document-id 3596 \
  --skip-document-id 3586 \
  --skip-document-id 3583
```

Import only built manifests:

```bash
docker compose run --rm -T \
  -v /opt/land-scout/genplan:/exports \
  web python - <<'PY'
import json
from pathlib import Path
from app.db import SessionLocal
from tools.genplan_import import import_release

summary = Path('/exports/ggk-shadow-batch-YYYYMMDD/lph-household/lph-household-summary.json')
with summary.open(encoding='utf-8') as f:
    rows = json.load(f)['results']

with SessionLocal() as session:
    for row in rows:
        if row.get('status') == 'built':
            import_release(session, Path(row['manifest']))
PY
```

Production run on 2026-07-31:

- `lph-household`: 69 shadow releases imported, 207 layers.
- `lph-field`: 12 shadow releases imported, 36 layers.
- `gardening`: 1 shadow release imported, 3 layers.

Never promote these rows by editing database flags. Rebuild a reviewed release
with `VERIFIED_STRICT` and `release_mode=search`.

Для Шымкента клиентская кнопка ручной проверки ведет на карту РГИС
`https://geo-shym.kz/map/?access_token=&lang=ru`. Постановление на Adilet
`https://adilet.zan.kz/rus/docs/P2300000916` используется как юридическое
основание в provenance/QA, но не как карта для клиента.

## Локальная разработка

```powershell
cd C:\Users\medadmin\Documents\Codex\2026-06-30\vj\land-scout-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Отдельно бот:

```powershell
python -m app.bot
```

Тесты:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest
```

## Перенос файлов с Windows на сервер

Production выполняется через `<ssh-user>@<production-host>`. Не подставлять старый `<old-private-host>`.

Пример через OpenSSH/scp, если ключ настроен:

```powershell
$server = "<ssh-user>@<production-host>"
$key = "C:\Users\medadmin\.ssh\id_ed25519_codex_land_scout"
$root = "/opt/land-scout/land-scout-bot"

scp -i $key app/main.py "${server}:${root}/app/main.py"
scp -i $key app/bot.py "${server}:${root}/app/bot.py"
scp -i $key app/auction_bot.py "${server}:${root}/app/auction_bot.py"
scp -i $key app/auction_service.py "${server}:${root}/app/auction_service.py"
```

Пример через PuTTY `pscp/plink`, если используется сохраненный host key:

```powershell
$server = "<ssh-user>@<production-host>"
$root = "/opt/land-scout/land-scout-bot"
$pscp = "C:\Program Files\PuTTY\pscp.exe"
$plink = "C:\Program Files\PuTTY\plink.exe"

& $pscp app\web.py "${server}:${root}/app/web.py"
& $pscp app\templates\site_login.html "${server}:${root}/app/templates/site_login.html"
& $pscp app\static\site.css "${server}:${root}/app/static/site.css"
& $plink $server "cd $root && sudo docker compose up -d --build web"
```

Пароли, токены и API keys не записывать в docs, Git и README.

После копирования:

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose up -d --build web worker bot
```

## Бэкап

### PostgreSQL

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose exec -T db pg_dump -U land_scout -d land_scout > land_scout_$(date +%Y%m%d_%H%M).sql
```

### Файлы проекта

```bash
cd /opt/land-scout
tar -czf land-scout-bot_$(date +%Y%m%d_%H%M).tar.gz land-scout-bot
```

Не публикуйте архивы с `.env`.

## Важные переменные production `.env`

Минимально проверить:

```dotenv
APP_ENV=production
APP_BASE_URL=https://zhertap.kz
POSTGRES_PASSWORD=
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_ADMIN_USER_IDS=
ADMIN_USERNAME=
ADMIN_PASSWORD=
INTERNAL_API_KEY=
TRIAL_ACCESS_ENABLED=true
TRIAL_ACCESS_DAYS=1
URBAN_PLAN_CHECK_MODE=strict
URBAN_PLAN_AUTO_WAIVE_UNAVAILABLE=true
URBAN_PLAN_RED_LINE_BUFFER_M=5
SMSC_ENABLED=true
SMSC_LOGIN=
SMSC_PASSWORD=
SMSC_BASE_URL=https://smsc.kz/sys/send.php
SMSC_SENDER=
APIPAY_ENABLED=true
APIPAY_API_KEY=
APIPAY_WEBHOOK_SECRET=
PLATFORM_ACCESS_PRICE_KZT=1990
AUCTION_ACCESS_PRICE_KZT=1990
PLATFORM_ACCESS_MONTHS=1
FREE_PREVIEW_PLOT_LIMIT=3
AUCTION_FREE_PREVIEW_LOTS=1
ANALYTICS_EXCLUDED_TELEGRAM_USER_IDS=70557953
```

## Типовые проблемы

### Бот не отвечает на `/start`

Проверить:

```bash
sudo docker compose logs --tail=150 bot
```

Причины:

- неверный `TELEGRAM_BOT_TOKEN`;
- контейнер `bot` упал;
- webhook Telegram мешает polling;
- бот был добавлен в группу и получает лишние апдейты;
- Telegram API временно недоступен.

### Веб-панель не открывается

Проверить:

```bash
sudo docker compose ps
sudo docker compose logs --tail=150 web
```

Причины:

- ошибка миграции БД;
- неверный `DATABASE_URL`;
- порт 8000 закрыт;
- `ADMIN_USERNAME`/`ADMIN_PASSWORD` изменены.

### В админке непонятный статус заявки

Сырой статус `ready` в БД не означает сам по себе "отчет отправлен" или
"участки найдены". Это техническое состояние: поиск завершился и заявка готова
к следующему шагу.

В `/admin` показываются человекочитаемые статусы через `admin_search_status()`:

- `Участки не найдены` - обычно `ready` + `search_outcome=no_candidates` или
  нет одобренных кандидатов; клиенту должен быть отправлен ответ без оплаты.
- `Найдено, ждёт отправки` - кандидаты есть, но отчет еще не доставлен или
  требуется действие оператора.
- `Ждёт оплаты` - найденные варианты есть, клиенту выставлен счет.
- `Оплачено, не отправлено` - платеж есть, но нужно проверить доставку.
- `Отчёт отправлен` - `delivered`, кандидаты отмечены доставленными.
- `Сбой поиска` - `failed`, смотреть `error_message` и логи worker/bot.

Telegram-клиенты не должны требовать ручного нажатия "Сформировать результат".
Если у `ready`-заявки есть `telegram_chat_id`, одобренные кандидаты и ни один
одобренный кандидат не отмечен `delivered_at`, `recover_stale_searches` через
`ensure_ready_delivery()` повторно запускает автоматическую доставку даже при
уже заполненном `search_completed_notified_at`. Это закрывает сценарий, когда
клиент увидел сообщение о завершении поиска, но превью/отчет не дошел.
Если в старых данных `free_preview_status=delivered`, но у кандидатов нет
`delivered_at`, восстановление переводит превью обратно в `pending` и отправляет
его повторно.

Заявки без `telegram_chat_id` - это админские предпросмотры или ручные проверки:
у них нет Telegram-адресата, поэтому автоматическая отправка клиенту невозможна.
На странице такой заявки не должно быть рабочей кнопки "Сформировать результат":
оператор смотрит результат прямо в панели, а UI объясняет, что отправлять
сообщение некому.

В деталях заявки нельзя выводить оператору только сырые технические статусы
кандидата (`rejected`, `blocked`, `waived`) и длинный текст `risk_notes` как
главное объяснение. Основной текст должен быть простым: подходит вариант,
отсеян генпланом/ПДП, нужна ручная сверка генплана или проверка завершилась
без подходящих мест. Технические поля оставлять только в раскрываемом блоке
"Техническое объяснение проверки".

Для диагностики конкретной заявки:

```bash
cd /opt/land-scout/land-scout-bot
docker compose exec -T db psql -U land_scout -d land_scout -P pager=off -c "
select
  id,
  status,
  search_outcome,
  payment_status,
  payment_provider_invoice_id,
  payment_confirmed_at,
  search_completed_notified_at,
  error_message
from search_requests
where id = '<REQUEST_ID>';
"

docker compose exec -T db psql -U land_scout -d land_scout -P pager=off -c "
select
  count(*) as candidates,
  count(*) filter (where review_status in ('approved','approved_with_note')) as approved,
  count(*) filter (where delivered_at is not null) as delivered
from candidates
where request_id = '<REQUEST_ID>';
"
```

### Аналитика оплат не сходится с поступлениями

Раздел `Сайт` в `/admin/analytics` считает только веб-аккаунтные платежи из
`account_payments`. Telegram-оплаты конкретных поисковых заявок считаются в
общей воронке поиска земли по `search_requests.payment_status='paid'`.

Бесплатные админские доступы (`payment_amount_kzt=0`, например
`admin:free-month`) не являются денежным поступлением и не должны считаться как
реальные оплаты сайта.

Проверить реальные оплаты Telegram-поиска за сегодня:

```bash
cd /opt/land-scout/land-scout-bot
docker compose exec -T db psql -U land_scout -d land_scout -P pager=off -c "
select
  id,
  telegram_user_id,
  payment_amount_kzt,
  payment_provider_invoice_id,
  payment_provider_status,
  payment_confirmed_at,
  status,
  search_outcome
from search_requests
where payment_status = 'paid'
  and (payment_confirmed_at at time zone 'Asia/Almaty')::date =
      (now() at time zone 'Asia/Almaty')::date
order by payment_confirmed_at desc;
"
```

Проверить реальные web-оплаты за сегодня:

```bash
docker compose exec -T db psql -U land_scout -d land_scout -P pager=off -c "
select
  ap.id,
  ap.account_id,
  ap.payment_amount_kzt,
  ap.payment_provider_invoice_id,
  ap.payment_provider_status,
  ap.payment_confirmed_at,
  ap.payment_confirmed_by
from account_payments ap
where ap.payment_status = 'paid'
  and coalesce(ap.payment_amount_kzt, 0) > 0
  and (ap.payment_confirmed_at at time zone 'Asia/Almaty')::date =
      (now() at time zone 'Asia/Almaty')::date
order by ap.payment_confirmed_at desc;
"
```

### Сайт открылся без стилей

Проверить, что CSS подключается относительным HTTPS-safe путем и отдается `200`:

```bash
curl -Ik https://zhertap.kz/static/site.css
curl -Ik https://zhertap.kz/static/app.css
```

Если менялся CSS/JS, увеличьте query string в шаблоне, например `?v=20260729c`, пересоберите `web` и очистите кэш браузера.

### SMS регистрации не отправляется

Проверить:

```bash
sudo docker compose logs --tail=150 web | grep -i smsc
```

Причины:

- `SMSC_ENABLED=false`;
- не заполнены `SMSC_LOGIN`/`SMSC_PASSWORD`;
- недостаточно баланса SMSC;
- SMSC временно недоступен;
- номер телефона не прошел нормализацию Казахстана.

В production вход после регистрации должен идти по паролю, а не по SMS каждый раз.

### ApiPay QR устарел

Пользователь должен нажать кнопку обновления ссылки оплаты. Backend отменяет/помечает старый invoice и создает новый.

Проверить:

```bash
sudo docker compose logs --tail=150 web worker bot | grep -i apipay
```

### E-Qazyna синхронизация неполная

В логах может быть:

```text
Skipping stale E-Qazyna deactivation: crawl incomplete
```

Это защитное поведение: если crawl не прошел полностью, система не деактивирует старые лоты, чтобы случайно не скрыть актуальные данные.

### Overpass 504/timeout

Система использует fallback URL и Celery retry. Если Overpass/OSM или публичный
ЕГКН временно не ответили, `land_scout.process_search` повторяет поиск до
лимита worker-а. Если лимит исчерпан и заявка ушла в `failed`, в карточке
`/admin/searches/{id}` есть кнопка "Повторить поиск": она создает новую заявку с
теми же параметрами через `retry_failed_search()` и сразу ставит ее в очередь.
Повторный клик не создает дубль, а открывает уже созданную повторную заявку.

### Заявка зависла или результат не дошел клиенту

Celery Beat запускает `land_scout.recover_stale_searches` каждые 5 минут:

- `processing` старше 15 минут переводятся обратно в очередь;
- pending free preview переотправляется;
- готовые ready/delivered уведомления, по которым не зафиксирована доставка, переотправляются;
- paywall после бесплатной выдачи восстанавливается, если не был показан.

Проверить:

```bash
sudo docker compose logs --tail=200 beat worker | grep -i recover
sudo docker compose exec worker celery -A app.tasks inspect active
```

Строгое правило эксплуатации: если ответ подготовлен, он не должен пропадать из-за зависания worker; recovery должен переотправить зависшее.

### Новая обратная связь не сверху

В `/admin/feedback` новые клиентские сообщения должны подниматься вверх и выделяться как непрочитанные. Открытие диалога фиксирует просмотр. Если стиль не виден, сначала проверить CSS-кэш и версию `/static/app.css`.

### Генплан отсутствует

Если `URBAN_PLAN_AUTO_WAIVE_UNAVAILABLE=true` и по выбранной территории в БД нет пригодного утвержденного слоя, система не останавливает клиента на кнопке. Заявка автоматически продолжает анализ по ЕГКН/OSM, получает статус генплана `waived`, а в отчете явно пишется, что генплан/ПДП не проверен из-за отсутствия цифрового слоя.

Важно: автопродолжение разрешено только для отсутствующего слоя. Если слой есть, но он сломан (`broken`), спорный, или кандидат попал в запретную зону/красную линию (`blocked`), координаты не должны выдаваться автоматически. В таких случаях нужно исправить/заменить слой или разбирать заявку вручную.

Покрытие фиксируется в таблице `urban_plan_coverage` по области, району, населенному пункту и семейству назначения. При повторном поиске `unavailable` читается из этой таблицы до тяжелой проверки слоев. При загрузке или включении слоя соответствующий кэш покрытия удаляется автоматически. Для диагностики:

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose exec -T db psql -U land_scout -d land_scout -c "select region,district,locality,purpose,coverage_status,approved_layer_count,checked_at from urban_plan_coverage order by checked_at desc limit 50;"
```

Если автоматический слой не найден, клиенту все равно показывается кнопка/ссылка для ручной сверки официального генплана или ПДП. Справочник таких ссылок хранится в `app/genplan_references.py`. Туда добавляются только официальные источники: страницы `adilet.zan.kz`, городские/областные геопорталы и Госградкадастр. Эта ссылка не означает, что система автоматически проверила генплан; она нужна, чтобы клиент мог сам открыть официальный источник.

Если для территории есть PDF/JPG/PNG/TIF в библиотеке ручных генпланов, клиенту
нужно давать именно файл карты, а не страницу Adilet с текстом постановления.
Индекс файлов хранится в `app/data/manual_genplans.json`, генерируется командой:

```powershell
.\.venv\Scripts\python.exe tools\build_manual_genplan_manifest.py
```

На production сами файлы должны лежать в корне `MANUAL_GENPLAN_FILES_ROOT`
или в стандартном пути `/opt/land-scout/manual-genplans/extracted`. Ссылка
выдается через `/manual-genplans/{asset_id}/{filename}`. Adilet остается
fallback-источником, если файла карты или геопортала нет.

### Клиент не понимает статус генплана

Проверить, что на сервер залиты `app/urban_plan_labels.py`, `app/web.py`,
`app/services.py`, `app/templates/site_search_detail.html`,
`app/static/site-search-status.js`, `app/static/site.css` и
`app/templates/site_base.html`.

Ожидаемое отображение:

- зеленый статус: `Генплан/ПДП проверен автоматически`;
- желтый статус: `Генплан/ПДП не подключен` / `нужна ручная сверка`;
- красный статус: `Генплан/ПДП показал ограничение`;
- нейтральный статус: `Генплан/ПДП ожидает проверки`.

Если страница `/cabinet/searches/{id}` показывает `Internal Server Error`,
сначала проверить логи `web`: шаблон должен получать `urban_plan_badge` через
общий `_cabinet_context`, а текущая заявка - `search_urban_plan_badge`.

## Безопасность эксплуатации

- Не отправлять токены и пароли в чат.
- Не хранить `.env` в Git.
- Не отдавать `INTERNAL_API_KEY` на внешний frontend.
- Для ApiPay нужен публичный HTTPS webhook.
- `APP_BASE_URL` в production должен быть `https://zhertap.kz`, иначе secure cookie/webhook/deep-link могут работать некорректно.
- Не деплоить на `172.*` адреса.
- Регулярно делать backup PostgreSQL.
- После изменения `.env` перезапускать контейнеры.
## Client-facing genplan links

Rule from 31.07.2026: do not show `adilet.zan.kz` as the primary genplan/PDP
action for clients. Adilet is a legal confirmation source, not a map.

User-facing priority:

1. Verified automatic urban-plan layer in the database.
2. Local PDF/JPG/PNG/TIF map from `app/data/manual_genplans.json`, served via
   `/manual-genplans/{asset_id}/{filename}`.
3. Interactive city/region map or geoportal.
4. General GGK geoportal for manual checking.

If only an Adilet legal article is known, the client must get the geoportal/manual
check button instead of the legal article.

## Automatic preliminary delivery without genplan

Rule from 01.08.2026: a client must not wait for an admin/user confirmation only
because the automatic genplan/PDP layer is missing or broken. If EGKN/OSM found
candidates and the urban-plan provider returns only `unavailable` decisions, the
request is auto-marked as `waived`, candidates are approved as preliminary, and
the report is delivered with a visible warning that genplan/PDP was not checked.

This does not bypass a real `blocked` result from an available verified layer:
if the loaded genplan/PDP layer shows a prohibited zone/red line, candidates are
still rejected.

Recovery guard: background recovery may resend only fresh stuck requests updated
within the last 6 hours and only when there is no previous delivery timestamp.
Do not run manual bulk delivery scripts for yesterday/older requests unless the
operator explicitly asks for a targeted user/request.
## Search result delivery and unpaid access

Rule from 01.08.2026: lack of payment, expired trial, or missing Telegram binding must not stop
the search result itself.

- Telegram users receive the automatic free/locked preview for every completed request. Exact
  coordinates, map links, EGKN links and cadastre references stay hidden until paid/trial access.
- Web users see every completed request in the cabinet even without Telegram binding. If the
  account has no paid/trial access, sensitive candidate fields are hidden by the web payload.
- Next batches in Telegram and web use `require_paid_access=False`; they may be requested in the
  locked mode too.
- Telegram auction catalog also uses locked mode instead of limiting unpaid users to one lot:
  every lot card can be opened, but E-Qazyna links, documents, favorites, subscriptions and
  comparison require paid/trial access.
- Admin queue must not show "waiting for Telegram delivery" for web-only requests. If
  `telegram_chat_id` is empty and candidates exist, the correct state is "result ready in cabinet".
- Do not run bulk delivery for yesterday/older requests unless targeting a specific user/request.

## Manual Genplan Processing Queue

PDF/JPG genplans are useful for client manual checking, but they are not an
automatic genplan/PDP check until converted into reviewed geospatial layers.

Build the current queue:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_status `
  --manual-manifest app\data\manual_genplans.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\status-report.json `
  --csv-output C:\Users\medadmin\Documents\Codex\genplan\work\status-report.csv
```

Scan raster files for embedded georeferencing before assigning manual work:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_embedded_scan `
  --inventory C:\Users\medadmin\Documents\Codex\genplan\inventory\manifests\manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\embedded-georef-scan.json
```

If `usable_embedded_georef` is 0 and `sidecar_world_file` is 0, the manual
library consists of plain images/PDFs and must pass through GCP placement and
QA before it can become an automatic genplan/PDP layer.

Audit bbox resolution before running heavy autoregistration:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_bbox_audit `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4
```

Current local audit from 03.08.2026:

- selected manual workbench records: 175;
- bbox resolved: 175;
- unresolved: 0;
- sources: 85 EGKN, 90 static city/district fallback, 0 Nominatim.

The static bbox fallback is only a search area for operator-assisted georeferencing. It does not mean the genplan/PDP has been automatically checked.

Run conservative autoreg over the current workbench manifest only to create diagnostics/proposed matching artifacts:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_batch `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v2 `
  --exclude-file C:\Users\medadmin\Documents\Codex\genplan\work\exclusions.json `
  --workers 2 `
  --max-tiles 144 `
  --min-free-disk-gb 0 `
  --max-output-gb 40 `
  --resume
```

The 2026-08-04 local v2 recheck completed all 175 current records with 0 failed assets, 0 pipeline-error assets, `registration_counts.needs_manual=175`, and `qa_or_strict_automatic=false`. Two very large JPEG plans (`�.����.jpg`, `�����������.jpg`) are decoded through a safe downsample path instead of failing the source-pixel guard. This is still not customer-search approval; it is preparation for A1/A2 operator QA.

Build the autoreg diagnostics CSV/JSON reports:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_autoreg_diagnostics `
  --autoreg-output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v2 `
  --workbench-manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v2
```

The current diagnostics summary is stored in `C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v2`: 175 assets, 350 attempts, 0 pipeline-error assets, 0 safe proposed GCP attempts, and 12 operator-only diagnostic anchor attempts.

When the workbench manifest is rebuilt with `--autoreg-output`, the local workbench shows links to `plan_preview`, `basemap`, `matches`, and the raw `result` for the best attempt. It also shows the diagnostic anchor count/quality where the conservative matcher found usable visual hints. The UI has a `Diagnostic anchors` filter and a `Load N anchors` button for those records. The button fills ordinary workbench GCP rows as an operator draft and scales coordinates from the matcher image to the currently displayed source image. These links and anchors are operator diagnostics only; do not turn them into approved GCPs without independent A1/A2 QA.

To seed all current operator-only diagnostic anchors as draft workbench GCP files:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_seed_diagnostic_gcps `
  --root C:\Users\medadmin\Documents\Codex\genplan `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\workbench_data `
  --summary C:\Users\medadmin\Documents\Codex\genplan\work\diagnostic-gcp-seed-v1\summary.json
```

The 2026-08-04 run seeded 12 draft records, skipped 0 existing records and had 0
errors. A repeat run skipped all 12 existing drafts. These drafts remain
`workflow_status=proposed`; they are not approved, not imported and not customer
search eligible until A1 verification, A2 review, export/vectorization and import
are complete.

The workbench record list also includes a local-only `Autoreg priority` filter. It sorts records by conservative matching score so the operator can start with maps that have more visual agreement with OSM/Esri. The score is only a triage helper; every raster/PDF genplan still needs manual A1 control points, QA, export/vectorization, A2 review, and import before it can affect customer checks.

Build local HTML operator packs from diagnostics:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_operator_packs `
  --diagnostics-dir C:\Users\medadmin\Documents\Codex\genplan\work\autoreg-diagnostics-v2 `
  --workbench-manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\operator-packs-v2 `
  --workbench-url http://127.0.0.1:8765 `
  --limit 50 `
  --pack-size 10
```

The v2 operator packs are in `C:\Users\medadmin\Documents\Codex\genplan\work\operator-packs-v2`. Open `index.html` locally to process the most promising 50 records in packs of 10.

Prepare safe one-page PDFs:

```powershell
& "C:\Users\medadmin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  -m tools.genplan_pdf_prepare `
  --inventory C:\Users\medadmin\Documents\Codex\genplan\inventory\manifests\manifest.json `
  --data-root C:\Users\medadmin\Documents\Codex\genplan `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-renders `
  --max-render-seconds 120
```

Run conservative autoregistration for prepared PNG files:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_batch `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-renders\manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-autoreg `
  --exclude-file C:\Users\medadmin\Documents\Codex\genplan\work\exclusions.json `
  --workers 2 `
  --max-tiles 64 `
  --min-free-disk-gb 5 `
  --max-output-gb 10 `
  --zoom 14 `
  --resume
```

Build a focused workbench manifest from the current queue:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_workbench_queue `
  --inventory C:\Users\medadmin\Documents\Codex\genplan\inventory\manifests\manifest.json `
  --status-report C:\Users\medadmin\Documents\Codex\genplan\work\status-report.json `
  --prepared-pdf-manifest C:\Users\medadmin\Documents\Codex\genplan\work\single-page-pdf-renders\manifest.json `
  --selected-pdf-page-manifest C:\Users\medadmin\Documents\Codex\genplan\work\selected-pdf-pages\manifest.json `
  --pdf-contact-sheet-manifest C:\Users\medadmin\Documents\Codex\genplan\work\pdf-contact-sheets\manifest.json `
  --bbox-audit-records C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4\records.json `
  --autoreg-output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-autoreg-v1 `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json
```

For multi-page PDFs, build contact sheets before rebuilding the workbench
manifest:

```powershell
& "C:\Users\medadmin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  -m tools.genplan_pdf_contactsheet `
  --inventory C:\Users\medadmin\Documents\Codex\genplan\inventory\manifests\manifest.json `
  --status-report C:\Users\medadmin\Documents\Codex\genplan\work\status-report.json `
  --data-root C:\Users\medadmin\Documents\Codex\genplan `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\pdf-contact-sheets `
  --dpi 40 `
  --columns 4 `
  --max-pages 80
```

Render operator-selected PDF pages and split multi-map PDFs into separate
workbench records before rebuilding the workbench manifest:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_pdf_page_select `
  --contact-sheet-manifest C:\Users\medadmin\Documents\Codex\genplan\work\pdf-contact-sheets\manifest.json `
  --selections app\data\genplan_pdf_page_selections.json `
  --data-root C:\Users\medadmin\Documents\Codex\genplan `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\selected-pdf-pages `
  --dpi 180 `
  --max-render-seconds 60
```

The current selection file picks Kaskelen page 22 and Semey page 19 as the
main drawings, and splits the Almaty PDP and Kyzylzhar/Roshchinsky PDFs into
separate map pages.

Open the local operator workbench:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_workbench `
  --root C:\Users\medadmin\Documents\Codex\genplan `
  --manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\workbench_data `
  --port 8765
```

Then open `http://127.0.0.1:8765`. The workbench UI is intentionally limited to
`proposed` and `qa_pending`; approved/importable layers still require review,
export and import.

The workbench has queue counters, a `Needs work` filter, status filters,
`Map area found` / `Map area needs review` bbox filters, and `Previous` /
`Next` navigation. Use `Needs work` for normal processing so maps with already
saved GCPs disappear from the active queue.
Large TIFF files are rendered server-side to PNG for display. Multi-page PDF
records show an `Open PDF contact sheet` link, then the operator selects the
correct page in the `Page` input before placing GCPs.

Build an operator CSV with direct workbench links:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_operator_queue `
  --status-report C:\Users\medadmin\Documents\Codex\genplan\work\status-report.json `
  --workbench-manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --bbox-audit-records C:\Users\medadmin\Documents\Codex\genplan\work\bbox-audit-v4\records.json `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\operator-queue.csv `
  --workbench-url http://127.0.0.1:8765
```

The CSV includes `bbox_status`, `bbox_source`, `bbox_label`, `workbench_url`,
`contact_sheet`, `duplicate_of`, and `queue_reasons`.

Build a readiness report for the full manual PDF/JPG/TIFF pipeline:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_readiness `
  --workbench-manifest C:\Users\medadmin\Documents\Codex\genplan\work\workbench-manifest.json `
  --workbench-output C:\Users\medadmin\Documents\Codex\genplan\workbench_data `
  --workbench-url http://127.0.0.1:8765 `
  --output C:\Users\medadmin\Documents\Codex\genplan\work\readiness-v7
```

The readiness report writes `summary.json`, `records.json`, and `records.csv`.
Use it as the daily control list: it separates bbox conflicts, PDF page
selection, A1 GCP placement, A2 review, export, vectorization, and import QA.
The 2026-08-03 local run produced 175 records after selected/split PDF page
rendering: 175 `gcp_needed`, 0 `bbox_review`, and no remaining
`page_selection` tasks. `records.csv` includes direct local workbench URLs.

If a record remains `manual_georeference_required`, open
`tools.genplan_workbench`, place control points, run independent review, export
with `tools.genplan_export`, vectorize with `tools.genplan_vectorize`, then
import only after QA. Never mark a PDF/JPG as automatically checked only because
the file exists.

## Production verification 2026-08-04

Latest production check was done against `<ssh-user>@<production-host>` in
`/opt/land-scout/land-scout-bot`.

Code parity checked by SHA-256:

- `app/providers/urban_plan.py`
- `app/live_search.py`
- `app/services.py`
- `app/schemas.py`
- `docker-compose.yml`

The hashes matched local files at the time of verification. Do not use
`172.*` private addresses for production checks or deploy.

Runtime state at verification:

- Docker services running: `web`, `bot`, `worker`, `auction_worker`, `beat`,
  `db`, `redis`.
- Redis queues: `critical=0`, `auctions=0`.
- `urban_plan_layers`: `396` total rows.
- Active client-search layers: `90`.
- Inactive/shadow rows: `306`.

Active strict/search rows by purpose:

| Purpose | allowed | prohibited | red_line |
|---|---:|---:|---:|
| LPH | 1 | 1 | 1 |
| LPH:household | 23 | 23 | 23 |
| Gardening | 6 | 6 | 6 |

Operational notes:

- `worker` listens to the `critical` queue for searches, payment reconciliation
  and recovery tasks.
- `auction_worker` listens to the `auctions` queue so E-Qazyna sync cannot block
  client search delivery.
- `app/providers/urban_plan.py` contains the spatial guard for broad metadata
  layers: a layer that does not actually cover candidate geometries must not
  block the whole request.
- `app/live_search.py` contains genplan-first prefiltering: active allowed
  genplan polygons restrict the EGKN search area when they overlap the selected
  district/locality.
- Detailed genplan status is in `docs/GENPLAN_STATUS_2026_08_04.md`.


