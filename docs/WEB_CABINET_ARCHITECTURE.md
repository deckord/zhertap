# Web cabinet architecture

## Goal

`https://zhertap.kz` is the public and private web layer for Land Scout Kazakhstan. It uses the same FastAPI app, PostgreSQL/PostGIS database, Celery workers, Telegram bot services, E-Qazyna sync and ApiPay webhook as the Telegram product.

The web layer must not duplicate business logic from Telegram. Search, auctions, payments, feedback and access checks should call shared backend services.

## Production

```text
Domain: https://zhertap.kz
Public IP: <production-host>
Project: /opt/land-scout/land-scout-bot
Runtime: Docker Compose
```

Do not deploy web changes to `<old-private-host>`, `<old-private-host>` or any other `172.*` private/local IP. Production is `<production-host>`.

## Product surfaces

- Public landing page `/`: value proposition, multi-step territory analysis, full report structure, data sources, limitations, pricing, 1-day trial and legal links.
- Legal pages: `/offer`, `/privacy`, `/terms`.
- Auth page `/login`: one visual auth panel with tabs for login and registration.
- Registration: phone -> SMS code -> password -> immediate web session.
- Login: phone + password, no SMS for every login.
- Forgot password: phone -> SMS code -> new password -> immediate web session.
- Client cabinet `/cabinet`: access status, Telegram link, recent searches and fresh auction lots.
- Help page `/cabinet/help`: plain-language instructions for territory analysis, data sources and result limitations.
- New-registration onboarding tour: optional interactive tour in the cabinet; users can skip it and it will not be shown again.
- Settings `/cabinet/settings`: profile, Telegram status, access status and password change.
- Web search `/cabinet/search`: same search profiles as Telegram, with region/district/settlement catalogs and “territory analysis” positioning.
- Search detail `/cabinet/searches/{id}`: live status polling, loading/progress state, visible analysis stages and candidates after worker finishes.
- Web auctions `/cabinet/auctions`: same backend filters, with catalog selectors.
- Auction detail, favorites, compare and subscriptions.
- Closed admin-only Auctions v2 `/cabinet/auctions-v2`: unified pre-purchase workspace for lots, sources, documents, cadastre, map, analytics, decision notes and diagnostics. Plain status: `docs/AUCTIONS_V2_STATUS.md`.
- Feedback `/cabinet/feedback`: client can write to the same feedback inbox as Telegram.
- Admin panel `/admin/*`: operator dashboard, auctions, feedback, urban plans and analytics.

## Identity model

The central identity is `accounts`.

- `phone` is the web login identifier.
- `password_hash` stores the password hash, never plaintext.
- `telegram_user_id` links the Telegram identity to the same account.
- `paid_access` and `access_granted_at` are permanent access state.
- `trial_started_at` and `trial_expires_at` are the 1-day trial state.
- `offer_version`, `offer_accepted_at`, `offer_accepted_ip`, `offer_accepted_user_agent` store legal acceptance.
- `onboarding_tour_available_at` marks accounts that should see the first-login tour.
- `onboarding_tour_dismissed_at` stores when the user skipped or completed the tour.
- `failed_login_attempts` and `locked_until` protect against brute force.

Supporting tables:

- `web_login_codes`: hashed one-time SMS codes with expiry and attempt counters.
- `web_sessions`: hashed session tokens for httpOnly cookies.
- `telegram_link_tokens`: short-lived tokens for linking Telegram from the web cabinet.
- `feedback_conversations`, `feedback_messages`, `feedback_broadcasts`, `feedback_broadcast_recipients`: client feedback and admin replies.

## Auth flow

Registration:

1. Client chooses the registration tab on `/login`.
2. Enters Kazakhstan phone number and accepts offer/terms.
3. Backend creates or loads `Account`, stores legal acceptance metadata and creates a `WebLoginCode`.
4. `app.sms.send_login_code` sends a code through SMSC.
5. Client enters SMS code, password and password confirmation.
6. Backend verifies the latest unconsumed code, hashes the password, marks phone verified, starts trial via `ensure_account_trial`, marks the onboarding tour available, creates `WebSession` and redirects to `/cabinet`.

Login:

1. Client chooses the login tab.
2. Enters phone and password.
3. Backend verifies password hash and starts a web session.
4. After 3 failed attempts, the account is locked for 5 minutes.

Duplicate registration:

- If `Account.phone` already exists and has `password_hash` or `phone_verified_at`, `/register/request-code` must not send SMS and must not create/overwrite registration state.
- The UI should tell the client that the number is already registered and direct them to login or forgot password.

Forgot password:

1. Client clicks "Забыли пароль?" on `/login`.
2. Backend checks that the account exists and has a password.
3. Backend creates `WebLoginCode` with `purpose=password_reset`.
4. SMSC sends the reset code.
5. Client enters SMS code and a new password.
6. Backend verifies only the latest unconsumed password-reset code, updates password hash, revokes existing web sessions and starts a new session.

Password change:

- `/cabinet/settings/password` requires current password and validates new password + confirmation.
- Failed current-password attempts also count toward temporary lock.

## Security

- SMS codes are stored only as server-side hashes.
- Passwords are stored as salted PBKDF2 hashes.
- Web sessions are stored server-side and browser cookies are `httpOnly`, `SameSite=Lax`, and `Secure` on HTTPS.
- After 3 failed verification/login attempts, the account is locked for 5 minutes.
- Registration codes and password-reset codes use separate `purpose` values and must not be interchangeable.
- Registration requires accepted offer/terms.
- ApiPay keys, SMSC credentials, Telegram tokens and database credentials stay only on backend.
- `APP_BASE_URL` must be `https://zhertap.kz` in production.

## Access

Access is unified across web and Telegram:

- paid access is permanent;
- paid Telegram users keep access after web account linking;
- `account_has_permanent_access` can copy existing Telegram paid state to the account;
- trial is only for non-paid accounts and does not override permanent access;
- `account_access_kind` returns `paid`, `trial` or `free`.

Default current policy:

```dotenv
TRIAL_ACCESS_ENABLED=true
TRIAL_ACCESS_DAYS=1
PLATFORM_ACCESS_PRICE_KZT=4990
AUCTION_ACCESS_PRICE_KZT=4990
```

## Telegram linking

1. User clicks "Привязать Telegram" in the cabinet.
2. Backend creates a short-lived token in `telegram_link_tokens`.
3. Website shows a bot deep-link with the token.
4. Bot consumes the token and writes `telegram_user_id` and `telegram_chat_id` to `accounts`.
5. Existing Telegram searches, favorites, subscriptions and access can be shown in web through account + Telegram user keys.

## Web search

Web search uses the same backend `create_search` and Celery `dispatch_search` flow as Telegram.

Important behavior:

- region/district/settlement are selected from catalogs, not free text;
- request is linked to `SearchRequest.web_account_id`;
- if Telegram is linked, `telegram_user_id` and `telegram_chat_id` are also stored;
- detail page polls `/cabinet/searches/{id}/status`, so users see progress without refreshing;
- detail page must present the process as multi-step analysis, not as an opaque “search is running” state;
- if worker is interrupted, recovery can requeue stale processing searches.

## Help and onboarding

The cabinet has a permanent `/cabinet/help` page. Its copy must stay non-technical and client-facing:

- explain that ЕГКН is used to read official territory boundaries, registered parcels and nearby cadastral orientirs;
- explain that the system searches for a gap where the requested plot size fits completely without crossing registered parcel borders;
- explain that open map data is used to screen obvious roads, buildings, water, cemeteries, industrial areas and other mapped objects;
- explain that official digital genplan/PDP layers are used, when available, to check allowed areas, red lines and prohibited zones;
- repeat that the result is preliminary and must be confirmed by akimat, land surveyor, documents and field inspection.

The interactive onboarding tour is implemented in `/static/site-onboarding.js` and is injected from `site_base.html` only when `show_onboarding_tour` is true. It is intentionally optional:

- first shown only for newly verified web registrations via `onboarding_tour_available_at`;
- skipped/completed through `POST /cabinet/onboarding/dismiss`;
- persisted on the account with `onboarding_tour_dismissed_at`;
- has a localStorage fallback only for a failed dismiss request;
- may navigate from `/cabinet` to `/cabinet/search` to highlight the actual form fields.

Current UX rule:

- use “анализ территории”, “расчетные варианты” and “полный отчет” across website and Telegram;
- do not claim a plot is officially free;
- do not show invented stage numbers such as “checked 1 842 plots” until the backend stores structured stage metrics and rejection reasons;
- live landing counters must come from the database, not from hardcoded marketing numbers.

## Web auctions

Web auctions use `auction_service` and existing E-Qazyna data.

There are two web surfaces:

- `/cabinet/auctions` - current user-facing auction section.
- `/cabinet/auctions-v2` - closed admin-only workspace for building the full process before official participation. It must guide the user through `find -> verify -> evaluate -> watch -> go to official portal`, not expose raw integration diagnostics as the main user flow.

Implemented:

- list/filter page;
- catalogs for region, district, locality and purpose;
- lot detail with metrics/history/changes;
- favorites;
- compare up to 10 favorite lots;
- subscriptions with enable/disable/delete;
- navigation/back links from detail, favorites and compare.

Auctions v2 implemented so far:

- admin-only access for `+77026669475`;
- v2 filters, catalog flow region -> district -> locality, active/future/archive/all scopes;
- lot detail with official source, documents, cadastre, map, history, risk, score and decision block;
- source status and diagnostics for administrator;
- document storage status foundation;
- watchlists and notifications foundation.

Remaining v2 work is tracked in `docs/AUCTIONS_V2_STATUS.md`.

## Feedback

Feedback is shared between web and Telegram.

Client surfaces:

- `/cabinet/feedback`;
- Telegram `/feedback`;
- response to feedback broadcast.

Admin surface:

- `/admin/feedback`.

Admin behavior:

- sees conversations, access status and payment/trial/free labels;
- sends broadcast to Telegram users using their latest language;
- can answer a client through Telegram;
- client replies are stored and shown in one thread;
- new client messages sort to the top and are highlighted as unread;
- opening a conversation marks it read.

## Deployment

The public site uses `/`; the admin dashboard remains under `/admin`. Nginx terminates HTTPS and proxies to FastAPI.

For web/template/CSS changes:

```bash
cd /opt/land-scout/land-scout-bot
sudo docker compose up -d --build web
```

If static assets are cached, bump the query string in templates, for example `/static/site.css?v=YYYYMMDDx`.
