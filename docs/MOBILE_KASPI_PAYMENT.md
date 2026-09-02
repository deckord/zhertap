# Мобильная оплата Kaspi из результатов поиска

## Назначение

Эта схема открывает оплату полного доступа из заблокированных полей результатов поиска: кадастрового ориентира, координат, карты, генплана и ЕГКН.

Целевое поведение одинаково на Android, iOS, мобильном браузере и desktop:

1. Пользователь нажимает **«Оплатить в Kaspi»**.
2. Открывается та же персональная `qr.kaspi.kz` ссылка, которая показана на `/cabinet/payment` кнопкой **«Открыть Kaspi»**.
3. Сумма уже закреплена в invoice ApiPay. Пользователь не вводит сумму вручную.
4. После webhook/polling со статусом `paid` открывается доступ.

На мобильных устройствах QR и модальное окно для этого сценария не нужны.

## Главный принцип

**Не строить мобильную оплату на модальном окне, `fetch`, JavaScript redirect или QR.**

Для активного счёта кнопка в поиске должна быть обычной ссылкой:

```html
<a href="https://qr.kaspi.kz/..." target="_blank" rel="noopener">Оплатить в Kaspi</a>
```

Это тот же механизм, что использует рабочая страница `/cabinet/payment`.

## Backend и данные

- `AccountPayment` хранит `payment_provider_url`, полученный из ApiPay `qr_token_url`.
- Только счёт со статусом `awaiting_transfer`, нетерминальным provider status и валидной `https://` ссылкой является кликабельным.
- В `search_detail()` в шаблон передаётся:

```python
direct_payment_url = _payment_context(latest_account_payment(session, account))["payment_url"]
```

- `site_search_detail.html` использует `direct_payment_url` для статичных карточек.
- `site-search-status.js` получает ту же ссылку через `data-payment-url` у корня страницы и использует её для карточек, перерисованных live-статусом.

## Fallback, если активной ссылки ещё нет

Если `direct_payment_url` отсутствует, разрешён резервный launcher:

- `POST /cabinet/payment/start-and-open` — обычная форма с CSRF, создаёт/переиспользует счёт и отдаёт `303` на `payment_url`;
- `GET /cabinet/payment/start-and-open` — совместимость со старыми/кэшированными мобильными ссылками и WebView. Он также создаёт/переиспользует счёт и отдаёт `303` на `payment_url`.

Нельзя оставлять этот URL только с `POST`: некоторые мобильные WebView открывают его как GET, что приводит к `405 Method Not Allowed` и зависанию на странице launcher.

## ApiPay quota и идемпотентность

ApiPay имеет ограничение на создание счетов. Поэтому:

1. Не создавать счёт при простом рендере страницы поиска.
2. Перед созданием искать активный pending invoice данного аккаунта и тарифа.
3. Повторные нажатия должны переиспользовать тот же pending invoice.
4. Создавать новый invoice только если старый terminal (`paid`, `cancelled`, `expired`, `error`) либо отсутствует.
5. Проверять фактический дневной расход в `account_payments`, а не предполагать исчерпание лимита.

Пример production-проверки:

```bash
docker exec land-scout-bot-db-1 psql -U land_scout -d land_scout -c "
SELECT (created_at AT TIME ZONE 'Asia/Almaty')::date AS day_almaty,
       count(*) AS invoices,
       count(*) FILTER (WHERE payment_provider = 'apipay') AS apipay_invoices
FROM account_payments
WHERE created_at >= now() - interval '4 days'
GROUP BY 1 ORDER BY 1 DESC;"
```

## Кэш frontend

`site-search-status.js` динамически перерисовывает карточки. При изменении HTML кнопки оплаты обязательно менять query-version этого скрипта в `site_search_detail.html`.

Текущая версия:

```text
/static/site-search-status.js?v=20260828-direct-link
```

Иначе браузер может загрузить новую HTML-страницу, а старый JS снова нарисует кнопки прежнего сценария.

## Диагностика инцидента

1. Проверить запросы web-контейнера:

```bash
docker logs --since 30m land-scout-bot-web-1 2>&1 | grep -E "payment/start-and-open|cabinet/searches"
```

2. Проверить последний invoice аккаунта: статус, provider status и наличие `qr.kaspi.kz` host. Не выводить полный token URL в логи/отчёты.
3. Для прямого mobile flow ожидается переход пользователя по прямой `payment_provider_url`, а не остановка на `zhertap.kz/cabinet/payment/start-and-open`.
4. Проверить `GET` и `POST` launcher routes в OpenAPI и `/ready` после деплоя.
5. Проверить тесты:

```bash
python -m py_compile app/web.py
python -m pytest -q \
  tests/test_direct_kaspi_payment.py \
  tests/test_search_direct_payment_url.py \
  tests/test_direct_kaspi_cache.py \
  tests/test_kaspi_get_launcher.py \
  tests/test_kaspi_new_context.py
```

## Запрещённые регрессии

- Не возвращать текст **«Скрыто до оплаты»** на кликабельных заблокированных полях.
- Не возвращать обязательный QR или модалку на mobile flow.
- Не использовать старый JS version после изменения динамической карточки.
- Не подставлять постоянный `PAYMENT_URL` вместо персонального `payment_provider_url` ApiPay.
- Не считать container health подтверждением оплаты: проверять DB invoice, ответ route и фактический target URL без раскрытия токена.
