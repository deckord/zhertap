# Zhertap Auctions Full TZ — master acceptance matrix

Дата контроля: 2026-09-02. Источники: `Downloads/Zhertap_Auctions_Full_TZ.docx`, текущий working tree проекта, production read-back 2026-09-01 18:30–18:35 UTC, `docs/CHANGELOG_LOGIC.md`, `docs/MASTER_SPEC_STATUS.md`, `docs/PROJECT_HANDOVER.md`, `docs/AUCTIONS_V2_STATUS.md`, `docs/AUCTIONS_V2_AGENT_PLAN.md`.

## Правило статуса

- **I — evidence-backed implemented**: есть data/model + logic + runtime route/task + focused test или проверенный runtime path.
- **P — partial/data-dependent**: часть вертикали есть, но отсутствует слой, интеграция, полнота данных, UI или production evidence.
- **D — declared/planned**: есть имя файла/класс/текст плана или заготовка, но нет доказанной пользовательской вертикали.
- **N — not implemented**: credible executable evidence не найдено.

Наличие файла/класса/миграции без route/task/data/test evidence не повышает статус выше D.

## Минимальные вертикальные релизы

| Релиз | Состав | Выходной пользовательский результат | Зависимости |
|---|---|---|---|
| **V0 Data Contract & Safety Gate** | 1–5, 8, 13, 20–23, 113–116 (минимум) | Лот с идентичностью объекта, нормализованными полями, source/date/confidence, честными unknown и STOP-over-rating | источники, миграции, source/evidence contract, auth/scope, CI |
| **V1 Pre-bid Decision MVP** | 6–19, 24–40 | Один лот: карта/граница, документы, извлечение с provenance, rule verdict, приватный checklist и следующий шаг | V0; E-Qazyna + EGKN/Jerler; worker; storage; route/UI |
| **V2 Market & Decision Economics** | 41–60 | История объекта, календарь/уведомления, verified comparables, сценарии и персональный/risk-adjusted STOP | V0–V1; реальный backfill; dedupe; sale/history completeness |
| **V3 Spatial Decision Pack** | 61–76 | Проверяемый GIS-пакет: ограничения, полезная площадь, подъезд, окружение, расхождения; unknown не превращается в pass | polygon identity, authoritative layers, spatial workers, map UI |
| **V4 Shared Due Diligence** | 77–88 | Владелец делится именно своим приватным DD с ролями, сроком и отзывом; эксперт комментирует и оставляет заключение | V1 private DD, ACL, file security, audit log |
| **V5 Field Inspection** | 89–95 | Мобильный осмотр с GPS/photo/voice/evidence и сравнением с автоматикой | V3 geometry/map, mobile-capable client, media storage, offline/retry |
| **V6 Post-win / Resale / Data Advantage** | 96–116 | Покупка→расходы→себестоимость→продажа→ROI, portfolio, consent/anonymization, export, expert marketplace | V2 decision snapshot + V4 security/audit + financial ledger; explicit consent |

## Матрица 116 пунктов

Формат: `№ | статус | evidence / acceptance gap`.

### Этап 1 — MVP (1–40)

1 | **P** | E-Qazyna current/history sync, hourly incremental Beat и durable recovery работают. Production read-back 2026-09-02 04:32 UTC: 28 133 lots, 199/199 active — land/`Прием заявок`, без null/past auction time, publication-after-auction и canonical identity gap. Durable status/date/page ledger исчерпал все 36 status/year-window cohorts. Пятиминутный bounded direct-detail sweep с durable keyset cursor теперь не позволяет сломанным новым `Running` cards навечно удерживать старые active rows; полный пустой проход подтверждён cursor=`{}`. Вертикаль остаётся P только потому, что текущие bounded crawls fail-closed отклоняют отдельные detail cards, которые E-Qazyna отдаёт страницей upstream database error; missing-lot deactivation не повышается до success.
2 | **I** | Conservative cross-source identity допускает только точный EGKN/cadastre/official Jerler key, отклоняет противоречия и покрыта collision/backfill tests. После исчерпания архива production содержит 27 294 земельных лота: 26 469 связаны, 825 честно оставлены unresolvable без стабильного официального ключа; повторный bounded backfill не создал догадочных связей.
3 | **I** | `AuctionLandObject` + `land_object_ref_id` дают единую identity торги→объект; exact-key collision/contradiction и repeat-publication regressions покрыты тестами. Production содержит разрешимые ссылки для всех 198 current active lots; active normalized history generation 285 reconciled 27 294/27 294 с errors=0.
4 | **P** | Поля есть в `AuctionLot`/normalization; исправлен live parser даты публикации и незаполненные строки принудительно refresh'ятся. Покрытие/provenance каждого обязательного поля по источникам всё ещё неполны.
5 | **I** | right-type labels, legal passport и карточка лота wired в v2 route/detail; unknown label сохраняется.
6 | **I** | v2 list/detail routes и templates показывают ключевые поля, risk/verdict; тесты `test_auctions.py`, `test_auction_v2.py`.
7 | **P** | Фильтры route/service есть (region/price/area/status/readiness/etc.), но право/срок/метод/дата и полный acceptance matrix фильтров не доказаны end-to-end.
8 | **I** | `auction_taxonomy.py` + user-facing purpose labels и tests.
9 | **I** | verdict/action labels разделяют participate/manual/watch/skip и insufficient data.
10 | **I** | Все 198 current active land lots имеют canonical identity и валидный официальный Jerler polygon; production targeted read-back 2026-09-02 подтвердил boundary 198/198, gaps=0. Геометрия не достраивалась по адресу/тексту.
11 | **I** | v2 map route, map JS и external map links есть; спутниковый слой — ссылка/внешний источник, не verified analysis.
12 | **P** | spatial fetch/worker/evidence store и OSM/EGKN metrics есть; набор расстояний и реальные populated responses зависят от координат/providers.
13 | **I** | evidence/status labels отделяют факт от вывода; acceptance требует запрета inference “ЛЭП рядом ⇒ подключение”.
14 | **P** | urban-plan context/checks подключены частично; нет полного lot-level PDP/genplan evidence по всем источникам.
15 | **P** | planning context/red-line checks существуют; ПДП для каждого требуемого объекта и слой инженерии не гарантированы.
16 | **I** | explicit `missing/manual_required/unknown` labels и dossier/card fallback.
17 | **P** | production downloader обновляет signed URL и скачивает в том же проходе; JPEG/PNG, ошибочно названные E-Qazyna как `.pdf`, безопасно нормализуются в настоящий PDF. Production batch: 95/100 downloaded; полный backlog и oversized/corrupt corpus остаются gate.
18 | **P** | PDF extraction + local LLM task/writer есть; schema coverage, OCR scans и real document corpus не подтверждены.
19 | **P** | extraction evidence/content hash/page-related state есть; end-to-end page/paragraph citation для каждого material claim не доказан.
20 | **I** | evidence/status vocabulary (`found/missing/manual_required/...`) и UI labels wired.
21 | **I** | deterministic verdict/rule modules and tests (`auction_verdict.py`, scenario/decision tests); production data completeness remains separate.
22 | **I** | risk labels and STOP-factor/action precedence exist in decision modules/tests.
23 | **I** | confidence/readiness labels exist; acceptance still requires source/date/confidence on every displayed conclusion.
24 | **P** | account/workspace/plan and private scope foundation exists; dedicated owner-created Due Diligence workspace/action is not evidenced.
25 | **P** | `auction_data_scope`, workspace members and document path scoping exist; cross-user negative tests for all personal materials/STOP price are required.
26 | **I** | `build_due_diligence_checklist` формирует characteristic-driven checklist из права, назначения, кадастра, документов, planning и ручных проверок; процент и критические открытые вопросы показаны в карточке.
27 | **P** | Общий `flood`/«Вода и паводок» manual check, status/note/upload route существуют. Исправлена географическая применимость NSDI WFS: `geonode:waterprotectionzone` является слоем только Костанайской области, а не национальным. Production active coverage 195/195: 188 `outside_published_coverage`, 1 `outside_published_extent`, 6 `no_intersection_in_published_layer`; все результаты остаются `manual_required`, missing/source_unavailable=0. Отдельные контракты водоохранной полосы и паводка/подтопления всё ещё отсутствуют.
28 | **D** | electricity check type exists; owner/voltage/protection/connection/relocation workflow absent.
29 | **D** | lease fields/labels exist; lease-specific checklist and request lifecycle absent.
30 | **D** | ownership labels exist; ownership checklist and obligations workflow absent.
31 | **P** | checklist динамически добавляет retail-проверку; профили туризма, производства, склада и остальных назначений с отдельными требованиями отсутствуют.
32 | **P** | manual checklist now writes an owner-scoped DD journal and maps `no_data/in_progress/done` to `draft/waiting/verified` when no file is attached; an uploaded answer is truthfully held at `received` until analysis/confirmation; external reference and explicit `risk` transition still need a dedicated UI.
33 | **P** | private/manual document storage foundation exists; generic user attachments/photo/screenshot/TU/letter/note UX and evidence linkage not proven.
34 | **P** | `auction_due_diligence_analysis.py` и worker извлекают bounded candidate facts из загруженного ответа с hash/status/provenance; полноценный OCR для сканов, LLM-разбор ответа и ручное подтверждение фактов остаются следующим шагом.
35 | **I** | Пункт ТЗ заменён продуктовым решением владельца: Zhertap не ищет орган и не генерирует обращение. Legacy POST-route генератора отключён с HTTP 410 и покрыт route-тестом; целевой сценарий — загрузка уже полученного ответа.
36 | **P** | Целевой сценарий сохранён: пользователь сам обращается в орган. Ручная проверка теперь всегда создаёт/обновляет owner-scoped журнал ответа, а загруженный файл привязывается к этой записи и анализируется; отдельный UI для номера обращения/срока ответа остаётся.
37 | **I** | dynamic checklist показывает completion percent, закрытые проверки и число критических открытых вопросов; unknown/manual не считаются выполненными.
38 | **I** | dossier text, verdict, risks, unknowns and action/next-step are wired in v2 detail.
39 | **I** | `PIPELINE_STAGES` и pipeline route покрывают watch/check/ready/participate/won/listed/sold; decision UI теперь отправляет только валидные `checking/watching/skipped`, регрессия `interested/rejected` закрыта тестом.
40 | **P** | activity/note/decision primitives and personal max fields exist; full contacts/calls/strategy/own-max UX and audit are incomplete.

### Этап 2 — investment analytics (41–60)

41 | **I** | calendar route показывает local datetime/countdown, гарантийный взнос, личный STOP и готовность ручных проверок; payload покрыт `tests/test_auction_calendar_payload.py`.
42 | **P** | watchlist notifications cover changes/new lots/deadlines in part; guarantee/check replies/documents and retry/delivery evidence need E2E tests.
43 | **I** | durable change/history и notification mapping покрывают price/date/purpose/guarantee/status/description/documents; guarantee/description добавлены в `CHANGE_TRACKED_FIELDS` с регрессионным тестом.
44 | **P** | document hashes/extraction states support versions; user-visible PDF diff/version history is not demonstrated.
45 | **P** | Active normalized-history generation 285 materialized/reconciled/activated ровно 27 294/27 294, `scan_complete=true`, errors=0; outcome quality: found 24 010, conflict 2 807, unknown 477. Внешний E-Qazyna archive также доказанно исчерпан по 36/36 status/date cohorts. Пункт остаётся P из-за 3 284 conflict/unknown outcomes и отсутствия доказанной пользовательской object timeline.
46 | **P** | history/change data can form a timeline; explicit object timeline UI and repeat-event identity are not verified.
47 | **P** | После полного archive ingest verified inventory содержит 307 current observations: 300 `found`/core-complete и 7 conflicts; единственный corpus — E-Qazyna results, 2021-02-05…2026-08-27. Все 239 target states остаются `insufficient`; строгий same-year engine корректно отказывает, но production geographic/year/right/purpose coverage недостаточно.
48 | **D** | Krisha/OLX/other market source ingestion is not evidenced; current market comparable primitives are not a live market vertical.
49 | **I** | карточка показывает каждый аналог, тип listing/verified, цену за сотку, роль и причину включения/исключения; MAD-выбросы маркируются явно.
50 | **P** | market metrics/estimate stores exist; liquidity definition and populated indicators are not proven.
51 | **I** | investment strategy enum and UI context exist; acceptance needs persisted user choice and effect on scenarios.
52 | **I** | Owner-scoped pipeline сохраняет strategy/purchase/exit/financing/holding/contingency/cost inputs и рассчитывает all-in cost, expected profit, ROI, margin и payback; route/persistence/portfolio regression покрывает полный deterministic example. Качество рекомендации всё ещё fail-closed зависит от verified market corpus.
53 | **N** | Существующие scenario rules описывают сценарии использования земли, а не финансовые пессимистичный/базовый/оптимистичный sensitivity-сценарии ТЗ.
54 | **P** | personal max/decision inputs foundation exists; owner-only persisted STOP editing and display are not fully evidenced.
55 | **P** | Decision snapshot/price ceiling рассчитывает fair value low/high и STOP либо честную блокировку. Active lots имеют snapshot 199/199, но non-stale current snapshot — 0/199; все 417 decision-input states `insufficient`, поэтому populated production STOP corpus отсутствует.
56 | **P** | risk-adjusted STOP отображает formula/readiness и конкретные missing/blocker/stale reasons из snapshot; требуется доказать на production, что ответы checklist меняют STOP по полному input contract.
57 | **I** | verdict separates parcel quality, data readiness and economic/action decision.
58 | **I** | watchlists route/model/tasks and notifications exist.
59 | **I** | strict market engine использует MAD threshold 3.5; UI явно помечает `outlier` и причину исключения.
60 | **P** | карточка показывает историю decision snapshots (verdict/readiness/STOP/current/stale) и текущие причины STOP; пользовательский журнал собственной причины/итога ещё неполон.

### Этап 3 — extended GIS (61–76)

61 | **P** | spatial fetch/evidence architecture exists; DEM source, terrain calculations and user-facing layer not found.
62 | **D** | NSDI water-protection-zone vertical не является flood history/model; проверенного слоя паводка/подтопления всё ещё нет.
63 | **P** | Официальный NSDI WFS `https://map.gov.kz/geoserver/ows` (`geonode:waterprotectionzone`, dataset 1633) имеет явный региональный contract для Костанайской области и production coverage 195/195 active lots с каноническим Jerler-полигоном. Только 6 участков внутри заявленной территории/extent получили `no_intersection_in_published_layer`; 188 вне региона и 1 вне extent честно отделены и все остаются `manual_required`. Пробелы: водоохранная полоса, остальные регионы и юридическая полнота/версия акта.
64 | **P** | source adapters/spatial checks can represent power lines; voltage/protection-zone source and polygon intersection not proven.
65 | **P** | geometry/area metrics exist in parts; “usable area” subtracting known restrictions with uncertainty is not a delivered UI result.
66 | **P** | Рассчитываются area/perimeter/bbox width-height/compactness/frontage и приблизительная depth; нет минимальной ширины/узких мест, UI и production validation на реальных polygon.
67 | **P** | road distance/OSM evidence exists; physical road vs legally confirmed access is not a complete two-status workflow.
68 | **D** | no reliable satellite feature-detection pipeline; external map links are not analysis evidence.
69 | **P** | decision input/evidence states support conflicts; automated registry-vs-satellite/GIS conflict detection absent.
70 | **D** | no historical satellite source/task/data evidence.
71 | **D** | no sufficient-data territorial dynamics model/report.
72 | **P** | neighboring/context layers can be fetched; no complete privacy-safe neighboring parcel analytics route.
73 | **P** | scenario taxonomy/context exists; differentiated surroundings per business purpose not complete.
74 | **D** | no competitive-surroundings data pipeline.
75 | **D** | no parcel subdivision geometry/options vertical.
76 | **D** | no compare-whole-vs-parts sale economics vertical.

### Этап 4 — shared review / experts (77–88)

77 | **P** | workspace/team routes and membership exist; sharing specifically an owner's private Due Diligence is not proven.
78 | **P** | access scope foundation exists; before/after-share visibility test for documents/checks is missing.
79 | **P** | workspace roles/member management exist; exact view/comment/co-review ACL semantics not acceptance-tested.
80 | **D** | no expiring share-link model/route for 24h/7d/30d/indefinite/revoke.
81 | **P** | invite member route exists; expert invite types and invite lifecycle are not complete.
82 | **P** | roles exist at workspace level; role-driven expert UI/check recommendations absent.
83 | **D** | no expert conclusion entity/route with author, role, date.
84 | **D** | no comments bound to checklist risk item.
85 | **D** | no comments bound to document/page/paragraph.
86 | **D** | no expert-created checklist item workflow.
87 | **P** | activity/decision records provide foundation; complete immutable audit of upload/comment/status-close is not proven.
88 | **P** | workspace scoping is a useful foundation; dedicated shared-access security tests and public-indexing guarantees required.

### Этап 5 — field inspection (89–95)

89 | **D** | `FIELD_INSPECTION_OPTIONS` is an enum only; no mobile GPS+polygon+distance route/data/test.
90 | **P** | `inspection_json`, статус выезда, несколько флагов и manual evidence upload существуют; полный мобильный checklist дороги/рельефа/воды/ЛЭП/мусора/шума/запаха отсутствует.
91 | **D** | no geotagged photo metadata/storage/route.
92 | **D** | no inspection map point entity/route.
93 | **D** | no voice upload/transcription/linkage task.
94 | **D** | no inspection report vertical.
95 | **D** | no automated-vs-field discrepancy comparison.

### Этап 6 — post-win / resale / data advantage (96–116)

96 | **P** | pipeline has won/contract/rights states; no dedicated “I won” transition workflow.
97 | **D** | no post-win checklist with protocol/contract/payments/registration evidence.
98 | **D** | no date-derived post-win deadline engine/reminders.
99 | **P** | fields/adapters for actual costs exist; no user-facing purchase financial card.
100 | **P** | actual-cost writer/adapters/tests exist; complete confirmed-expense ledger UX is absent.
101 | **P** | `auction_actual_cost_*` and models support calculation; no complete purchase+all-linked-cost acceptance path.
102 | **D** | no listing/lead/price-change/sale-date workflow.
103 | **D** | no net-profit calculation from realized sale and taxes/expenses.
104 | **D** | no ROI/holding-period report.
105 | **D** | no hypothesis-vs-STOP-vs-actual outcome analysis.
106 | **P** | portfolio route/template and pipeline states exist; purchased/for-sale/sold financial result coverage is not proven.
107 | **N** | no explicit consent-to-anonymize-and-contribute workflow.
108 | **N** | no verified anonymization pipeline removing personal/private identifiers before reuse.
109 | **D** | incentives explicitly later; no implementation evidence.
110 | **D** | no expert catalog entity/search/route.
111 | **D** | no paid expert order/payment/delivery flow reusing DD.
112 | **P** | dossier text and admin CSV exist; requested PDF report with maps/legal/docs/market/STOP is not implemented.
113 | **P** | code separates auction/history/evidence/documents/spatial/decision modules, but target `system_checks`, `user_due_diligence`, `shared_access` and complete data contract are not delivered.
114 | **P** | activity/change/snapshot audit foundations exist; critical-data and consent audit is incomplete.
115 | **P** | storage path scoping, private workspace and download checks exist; signed/revocable links, malware/content controls and negative ACL tests are not proven.
116 | **I** | Decision evidence contract `2026.2`/snapshot engine `2026.3` сохраняет explicit unknown/risk/blocker/action/reason/evidence refs без придуманного ceiling. Live-container read-back с отсутствующими семью required modules вернул `manual_required`, семь unknown facts, полный reason list и `ceiling=null`; AI/LLM не участвовал.

## Технический completion/gap audit — 2026-09-02

Полный пересчёт всех 116 строк выше: **I 28 / P 54 / D 31 / N 3**.
Ни наличие файла, ни queued task, ни пустая таблица не считались реализацией.

- **I (28):** 2, 3, 5, 6, 8, 9, 10, 11, 13, 16, 20, 21, 22, 23, 26, 35, 37, 38, 39, 41, 43, 49, 51, 52, 57, 58, 59, 116.
- **P (54):** 1, 4, 7, 12, 14, 15, 17, 18, 19, 24, 25, 27, 31, 32, 33, 34, 36, 40, 42, 44, 45, 46, 47, 50, 54, 55, 56, 60, 61, 63, 64, 65, 66, 67, 69, 72, 73, 77, 78, 79, 81, 82, 87, 88, 90, 96, 99, 100, 101, 106, 112, 113, 114, 115.
- **D (31):** 28, 29, 30, 48, 62, 68, 70, 71, 74, 75, 76, 80, 83, 84, 85, 86, 89, 91, 92, 93, 94, 95, 97, 98, 102, 103, 104, 105, 109, 110, 111.
- **N (3):** 53, 107, 108.

### Обязательная техническая последовательность до visual/UI

1. **E-Qazyna current/status/time/deadline — ARCHIVE EXHAUSTED, CURRENT PARTIAL.**
   Исторический ledger доказал empty-page exhaustion для всех `36/36` status/date
   cohorts; последний run `658743b3c1ca4d36aa4ed4282ebd19d1` завершён без detail
   work и сохранил все checkpoints=`0`. Текущий active set согласован (`198/198` land:
   190 принимают заявки, 2 в регистрации/приёме, 6 уже проводятся; state/time пары
   непротиворечивы). Отдельный пятиминутный
 direct-detail sweep имеет durable keyset cursor и доказанно не допускает starvation
 старых `Running` rows даже при постоянных ошибках новых cards. Основной current crawl
 остаётся fail-closed на официальных detail cards, которые E-Qazyna подменяет
 database-error page. Деактивация отсутствующих лотов поэтому не считается завершённой.
2. **Canonical identity/Jerler polygon — ACTIVE COMPLETE, ARCHIVE IDENTITY PARTIAL.** Все 198
   active lots связаны с canonical object и имеют официальный Jerler polygon (`198/198`);
   по полному архиву `26 469/27 294` связаны, а 825 честно unresolvable без официального ключа.
3. **EGKN/NSDI/official GIS — ONE REGIONAL OFFICIAL VERTICAL COMPLETE, BROADER PIPELINE BLOCKED.**
   Исправлен критичный provenance/coverage defect: NSDI `geonode:waterprotectionzone`
   официально называется слоем Костанайской области, а не национальным. Контракт
   `nsdi-regional-coverage/2026.1` требует совпадения региона и полного bbox внутри
   опубликованного extent. Production active read-back `195/195`: 188
   `outside_published_coverage`, 1 `outside_published_extent`, 6
   `no_intersection_in_published_layer`; все 195 остаются `manual_required`,
   `source_unavailable=0`. Task зарегистрирован на auction worker и Beat каждые 900 секунд.
   Общая signed spatial pipeline всё ещё выключена и имеет `0` feed
   states/manifests/expectations/signals; water strip, flood, power, red-line и municipal GIS
   feeds не подключены.
4. **Archive/history — SOURCE/GENERATION COMPLETE, QUALITY PARTIAL.** E-Qazyna archive
   исчерпан по `36/36` cohorts; active generation 285 закрыла `27 294/27 294` без ошибок.
   Качественный gap: `2 807 conflict + 477 unknown` outcomes; они не переименованы в
   verified history.
5. **Strict same-year verified comparables — ENGINE COMPLETE, CORPUS BLOCKED.** Inventory
   вырос после полного archive ingest до `307`: core-complete/found `300`, conflicts `7`,
   но `239/239` targets остаются insufficient.
6. **Official territory project/news — DURABLE CONTRACT/RUNTIME COMPLETE, CORPUS BLOCKED.**
   Immutable observation revisions, source/date/authority/content hashes, whole-parcel
   applicability and bounded auction-worker linkage are deployed at Alembic head
   `c2f6a8d1e4b9`. Same-revision content conflicts and lifecycle regressions fail closed;
   only an official polygon covering the whole parcel can become `applicable`. Production
   read-back remains `0` observations / `0` applicability because no trusted structured
   corpus currently satisfies the contract; prose/news without official polygon remains
   `manual_required` and is not imported into this store.
7. **Decision evidence contract — CONTRACT/RUNTIME COVERAGE COMPLETE, READINESS BLOCKED.**
   После bounded recompute все `198/198` active land lots имеют current snapshot точных
   engine/rules versions. Все 198 честно `stale=true`, `data_readiness=partial` и без ceiling:
   185 `manual_required/requires_check`, 13 `blocked/do_not_participate`; у 198/198 есть
   explicit unknown, action и полный reason list. Active decision-input states также
   `198/198 insufficient` (по всему архивному state-store `417 insufficient`): основные
   gaps — restriction/site/planning/market/legal modules и actual-cost corpus. Нельзя
   выдавать production STOP или ceiling как готовый инвестиционный вывод.
8. **116-point acceptance — AUDITED, NOT ACCEPTED.** V0–V6 не проходят release-level
   criteria из-за перечисленных data/runtime gaps и незакрытых P/D/N пунктов. Visual/UI
   redesign не разрешён до их закрытия.

### Runtime/release blockers

- Production checkout не воспроизводим: HEAD `ea3bea7...`, marker `.codex_deployed_sha`
  указывает `ae43839...`, checkout содержит 137 tracked modifications и 298 untracked
  status entries. Running-container hashes совпадают с dirty checkout, но не с
  зафиксированным release revision.
- `/health` и `/ready` возвращают HTTP 200, Alembic `b0c5d8e1f3a7 (head)`, все девять
  containers running; однако `ollama` имеет исторический `OOMKilled=true`.
- `AUCTION_V2_LLM_ENABLED=false` и
  `AUCTION_V2_DOCUMENT_EXTRACTION_ENABLED=false` подтверждены в `web`,
  `auction_worker` и `worker`. Backlog extraction не обрабатывается и не считается
  завершённым.
- Перед NSDI production backfill создан и проверен backup
  `/opt/land-scout/backups/pre_nsdi_water_20260902T014818Z.dump` (654 224 442 bytes,
  SHA-256 `36711ea1edb12393f1e3312b56c2d2f5a6423d5301aeea9829267c38cba289f5`).
  Focused NSDI/spatial suite: `22 passed`; после bounded backfill coverage `166/166`,
  `source_unavailable=0`. Пересобраны только `auction_worker` и `beat`; task зарегистрирован,
  schedule=900, `/health` и `/ready` HTTP 200, source/container hashes совпадают.
- Перед territory-observation migration создан backup
  `/opt/land-scout/backups/pre_territory_store_20260902T022746Z.dump`
  (654 301 957 bytes, SHA-256
  `7a658321029a33e2af58eaead03541c49685b552b305e1e97b4fa4f5fdbc9e1c`).
  Первый build обнаружил 100% disk usage; удаление только неактивного Docker build cache
  освободило 17.1 GB. Alembic `c2f6a8d1e4b9 (head)` применён, пересобраны только `web`,
  `auction_worker`, `beat`; task зарегистрирован, `/health` и `/ready` HTTP 200,
  checkout/container hashes совпадают. Transactional smoke rolled back cleanly; production
  corpus честно остаётся `0/0`, AI/document extraction flags — `false,false`.
- Полный локальный acceptance run (Python 3.12, explicit native `PYTHONPATH`) после
  regression patch завершился `1219 passed, 1 skipped` за 1010.33 s. Закрыты четыре
  catalog contract regression (official EGKN/manual fallback + E-Qazyna count overlay),
  failed-document priority, legacy SMS password-reset/rate-limit compatibility и
  недетерминированная календарная дата paid Telegram access test. Ранее найденная
  security-регрессия trial status payload остаётся fail-closed: focused privacy/
  free-preview suite прошёл `15 passed`, production read-back возвращает для trial
  preview только `null` в sensitive fields и `locked=true`. Production пересобрал только
  затронутые `web` и `auction_worker`; `/health` и `/ready` HTTP 200, source/container
  hashes совпадают, AI/document extraction flags остаются `false,false`. Первый запуск
  без native `PYTHONPATH` дал 45 collection errors и не используется как результат.
- NSDI geographic-applicability correction: live WFS capabilities доказали, что
  `geonode:waterprotectionzone` — «Водоохранная зона Костанайской области» с extent
  `60.5875554,48.7392174…68.1003206,54.6466798`, dataset 1633. Focused suite после
  test-first patch: `11 passed`. Перед bulk refresh создан backup
  `/opt/land-scout/backups/pre_nsdi_coverage_fix_20260902T060832Z.dump`
  (668 636 129 bytes, SHA-256
  `707a776709f813e969a456aa71797f02c80747c47e73c301fb36f32f63305eac`).
  Пересобран только `auction_worker`; bounded refresh исчерпан (`selected=0`).
  Read-back: active/canonical/evidence/contract `195/195/195/195`, 188 outside region,
  1 outside extent, 6 in-coverage no-intersection, non-Kostanay empty-result errors=0.
  `/health` и `/ready` HTTP 200, container hashes совпадают, AI/document extraction
  flags=`false,false`.
- Overdue-status sweep: focused time/status/deadline suite `5 passed`; task зарегистрирован
  только на `auction_worker`, Beat schedule=300 секунд, durable cursor после полного пустого
  прохода=`{}`. Production read-back: active `199`, land/ApplicationsAccept `199/199`,
  null/past auction time, publication-after-auction и canonical identity gaps=`0`.
  Checkout/container hashes `auction_service.py=842c06f8...`, `tasks.py=72a70a80...`;
  `/health` и `/ready` HTTP 200, Alembic `c2f6a8d1e4b9 (head)`, AI/document extraction
  flags=`false,false`. Это закрывает stale-active starvation, но не upstream database-error
  blocker текущего crawl.

## Release-level acceptance — критерий “готово”

**V0 is ready only when** a fixture lot passes source→identity→normalized fields→evidence (source, observed_at, confidence)→unknown/STOP rules, migration applies, and negative tests show no invented positive conclusion.

**V1 is ready only when** a real or deterministic fixture can be opened through the user route and answer: official link/deadline, right/term, boundary/area, map/context, documents, extracted claims with page evidence, checked/missing items, deterministic verdict, private owner workspace, and next action. Worker retries and empty-provider states are observable.

**V2 is ready only when** backfill counters by status/date/page are reconciled, repeat lots map to object identity, history/comparables are populated, and a user can reproduce calendar→notification→scenario→personal STOP with insufficient-data fallback.

**V3 is ready only when** polygon fixtures cover each spatial layer and show intersection/area/distance plus source/date/confidence; missing layer yields unknown/manual, never pass; map UI and spatial workers are wired and tested.

**V4 is ready only when** owner A shares a DD to B with each role/expiry, B cannot read it before access, can perform only allowed actions after access, revoke immediately removes access, and audit records every critical action.

**V5 is ready only when** mobile/offline/retry flow stores GPS/photo/voice/time/category, generates a report, and highlights an intentional contradiction against automation.

**V6 is ready only when** realized financial ledger recomputes cost/profit/ROI, portfolio and sale states are user-visible, consent precedes anonymization, export is access-controlled, and audit/security tests pass.

## Главные зависимости и риски

1. **Source completeness is the critical path**: current docs explicitly call E-Qazyna historical backfill partial; no analytics release should claim “market history” without counters and coverage report.
2. **Object identity before history**: without stable land-object identity, repeat auctions, timeline, comparables and financial outcomes can be silently merged incorrectly.
3. **Evidence contract before AI/UI**: LLM extraction cannot satisfy GIS/legal/investment checks; every conclusion needs source/date/confidence and page/fragment where applicable.
4. **Private DD/ACL before sharing/files**: existing workspace/team primitives are not proof of owner-private DD. Add negative cross-account tests before V4/V6.
5. **Unknown is a first-class outcome**: `missing/manual_required/planned` labels are present, but acceptance must inspect persisted payloads and verdict gates, not only templates.
6. **Working-tree caveat**: branch `codex/release-20260807` has extensive modified and untracked implementation files; this review is of the current working tree, not a clean deployed revision. No commit/deployment claim is made.
7. **Verification limitation**: targeted pytest invocation could not run because the active environment has no `pytest` module. `python -m compileall -q app tests` also fails on existing syntax incompatible with Python 3.11: `app/provider_guard.py:105` uses `def guarded_http_call[T]` (Python 3.12 syntax). Therefore test pass claims in docs were not independently reproduced here.
