# Zhertap Auctions — оставшиеся пункты ТЗ

Дата аудита: 2026-08-25.

Всего: 116; закрыто I: 23; осталось: 93 (P: 59, D: 31, N: 3).

Статусы: P — частично/data-dependent; D — foundation/planned; N — не реализовано.

## Этап 1 — MVP — осталось 24

- **1. Автоматический сбор лотов — P.** E-Qazyna current/history sync, hourly incremental Beat, durable recovery и production backfill подтверждены; первая reconciled generation закрыла 2 103 лота, следующий run пропускает уже полные lot IDs. Полная source exhaustion по каждому status/date/page всё ещё не доказана из-за page/detail caps.
- **2. Связка источников — P.** E-Qazyna/Jerler/ЕГКН adapters и object enrichment есть; нет доказанного стабильного cross-source match по EGKN ID для реального набора.
- **3. Единый земельный объект — P.** `AuctionLot`, `AuctionLotHistory`, repeat labels/backfill есть; единая identity торги→объект и collision tests не закрыты.
- **4. Нормализация — P.** Поля есть в `AuctionLot`/normalization; исправлен live parser даты публикации и незаполненные строки принудительно refresh'ятся. Покрытие/provenance каждого обязательного поля по источникам всё ещё неполны.
- **7. Фильтры — P.** Фильтры route/service есть (region/price/area/status/readiness/etc.), но право/срок/метод/дата и полный acceptance matrix фильтров не доказаны end-to-end.
- **10. Polygon участка — P.** координаты/частичная геометрия и `auction_map_projection` есть; обязательное получение polygon и PostGIS coverage не гарантировано.
- **12. Базовый GIS — P.** spatial fetch/worker/evidence store и OSM/EGKN metrics есть; набор расстояний и реальные populated responses зависят от координат/providers.
- **14. Генплан — P.** urban-plan context/checks подключены частично; нет полного lot-level PDP/genplan evidence по всем источникам.
- **15. ПДП — P.** planning context/red-line checks существуют; ПДП для каждого требуемого объекта и слой инженерии не гарантированы.
- **17. Документы лота — P.** production downloader обновляет signed URL и скачивает в том же проходе; JPEG/PNG, ошибочно названные E-Qazyna как `.pdf`, безопасно нормализуются в настоящий PDF. Production batch: 95/100 downloaded; полный backlog и oversized/corrupt corpus остаются gate.
- **18. AI-анализ PDF — P.** PDF extraction + local LLM task/writer есть; schema coverage, OCR scans и real document corpus не подтверждены.
- **19. Ссылки на основания — P.** extraction evidence/content hash/page-related state есть; end-to-end page/paragraph citation для каждого material claim не доказан.
- **24. Персональный Due Diligence — P.** account/workspace/plan and private scope foundation exists; dedicated owner-created Due Diligence workspace/action is not evidenced.
- **25. Приватность — P.** `auction_data_scope`, workspace members and document path scoping exist; cross-user negative tests for all personal materials/STOP price are required.
- **27. Checklist воды — P.** общий `flood`/«Вода и паводок» manual check, status/note/upload route существуют; отдельные evidence-состояния водоохранной зоны, полосы и паводка/подтопления не реализованы.
- **28. Checklist ЛЭП — D.** electricity check type exists; owner/voltage/protection/connection/relocation workflow absent.
- **29. Checklist аренды — D.** lease fields/labels exist; lease-specific checklist and request lifecycle absent.
- **30. Checklist собственности — D.** ownership labels exist; ownership checklist and obligations workflow absent.
- **31. Checklist по назначению — P.** checklist динамически добавляет retail-проверку; профили туризма, производства, склада и остальных назначений с отдельными требованиями отсутствуют.
- **32. Статусы проверки — P.** pipeline statuses and manual check states exist; requested checklist state machine (`request/sent/waiting/verified/risk`) not complete.
- **33. Вложения пользователя — P.** private/manual document storage foundation exists; generic user attachments/photo/screenshot/TU/letter/note UX and evidence linkage not proven.
- **34. AI-анализ ответов — P.** `auction_due_diligence_analysis.py` и worker извлекают bounded candidate facts из загруженного ответа с hash/status/provenance; полноценный OCR для сканов, LLM-разбор ответа и ручное подтверждение фактов остаются следующим шагом.
- **36. Учёт обращения — P.** Целевой сценарий — пользователь загружает уже полученный ответ через ручную проверку; приватное хранение и анализ документов существуют, но единый generic response-upload UX без журнала обращений ещё требует упрощения.
- **40. Заметки — P.** activity/note/decision primitives and personal max fields exist; full contacts/calls/strategy/own-max UX and audit are incomplete.

## Этап 2 — инвестиционная аналитика — осталось 13

- **42. Напоминания — P.** watchlist notifications cover changes/new lots/deadlines in part; guarantee/check replies/documents and retry/delivery evidence need E2E tests.
- **44. Версионность PDF — P.** document hashes/extraction states support versions; user-visible PDF diff/version history is not demonstrated.
- **45. История торгов — P.** Object history, normalized history, durable backfill и active reconciled generation 58/2 103 доказаны production; nullified prices исключены, target repeats исключены из comparables. Полная archive exhaustion и publication-date backfill ещё выполняются.
- **46. Timeline объекта — P.** history/change data can form a timeline; explicit object timeline UI and repeat-event identity are not verified.
- **47. Аналоги аукционов — P.** comparables/verified sales/repository and market tasks exist; source population and median correctness are data-dependent.
- **48. Рыночные аналоги — D.** Krisha/OLX/other market source ingestion is not evidenced; current market comparable primitives are not a live market vertical.
- **50. Ликвидность — P.** market metrics/estimate stores exist; liquidity definition and populated indicators are not proven.
- **52. Флиппинг — P.** price ceiling, actual-cost adapters/writer and market estimate exist; sale/expense inputs and complete flip margin workflow absent.
- **53. Сценарии — N.** Существующие scenario rules описывают сценарии использования земли, а не финансовые пессимистичный/базовый/оптимистичный sensitivity-сценарии ТЗ.
- **54. Мой STOP — P.** personal max/decision inputs foundation exists; owner-only persisted STOP editing and display are not fully evidenced.
- **55. Рекомендуемый STOP — P.** decision snapshot/price ceiling рассчитывает fair value low/high и STOP; карточка показывает значения или честную блокировку. Полный populated production corpus с verified sales остаётся data gate.
- **56. Risk-adjusted STOP — P.** risk-adjusted STOP отображает formula/readiness и конкретные missing/blocker/stale reasons из snapshot; требуется доказать на production, что ответы checklist меняют STOP по полному input contract.
- **60. История решений — P.** карточка показывает историю decision snapshots (verdict/readiness/STOP/current/stale) и текущие причины STOP; пользовательский журнал собственной причины/итога ещё неполон.

## Этап 3 — расширенный GIS — осталось 16

- **61. Рельеф — P.** spatial fetch/evidence architecture exists; DEM source, terrain calculations and user-facing layer not found.
- **62. Паводковый GIS — D.** no verified flood GIS/official zones/history vertical.
- **63. Водоохранные слои — P.** spatial evidence and restriction context can represent water layers; authoritative source/area-of-restriction output not proven.
- **64. ЛЭП и охранные зоны — P.** source adapters/spatial checks can represent power lines; voltage/protection-zone source and polygon intersection not proven.
- **65. Полезная площадь — P.** geometry/area metrics exist in parts; “usable area” subtracting known restrictions with uncertainty is not a delivered UI result.
- **66. Форма — P.** Рассчитываются area/perimeter/bbox width-height/compactness/frontage и приблизительная depth; нет минимальной ширины/узких мест, UI и production validation на реальных polygon.
- **67. Подъезд — P.** road distance/OSM evidence exists; physical road vs legally confirmed access is not a complete two-status workflow.
- **68. Спутниковый анализ — D.** no reliable satellite feature-detection pipeline; external map links are not analysis evidence.
- **69. Расхождения — P.** decision input/evidence states support conflicts; automated registry-vs-satellite/GIS conflict detection absent.
- **70. Исторический спутник — D.** no historical satellite source/task/data evidence.
- **71. Динамика территории — D.** no sufficient-data territorial dynamics model/report.
- **72. Соседние участки — P.** neighboring/context layers can be fetched; no complete privacy-safe neighboring parcel analytics route.
- **73. Окружение по назначению — P.** scenario taxonomy/context exists; differentiated surroundings per business purpose not complete.
- **74. Конкурентное окружение — D.** no competitive-surroundings data pipeline.
- **75. Потенциал разделения — D.** no parcel subdivision geometry/options vertical.
- **76. Разделить и продать — D.** no compare-whole-vs-parts sale economics vertical.

## Этап 4 — совместная проверка и эксперты — осталось 12

- **77. Поделиться лотом — P.** workspace/team routes and membership exist; sharing specifically an owner's private Due Diligence is not proven.
- **78. До/после шаринга — P.** access scope foundation exists; before/after-share visibility test for documents/checks is missing.
- **79. Права доступа — P.** workspace roles/member management exist; exact view/comment/co-review ACL semantics not acceptance-tested.
- **80. Срок ссылки — D.** no expiring share-link model/route for 24h/7d/30d/indefinite/revoke.
- **81. Приглашение пользователя — P.** invite member route exists; expert invite types and invite lifecycle are not complete.
- **82. Роли экспертов — P.** roles exist at workspace level; role-driven expert UI/check recommendations absent.
- **83. Экспертное заключение — D.** no expert conclusion entity/route with author, role, date.
- **84. Комментарии к рискам — D.** no comments bound to checklist risk item.
- **85. Комментарии к документам — D.** no comments bound to document/page/paragraph.
- **86. Новый checklist от эксперта — D.** no expert-created checklist item workflow.
- **87. История действий — P.** activity/decision records provide foundation; complete immutable audit of upload/comment/status-close is not proven.
- **88. Приватность совместной работы — P.** workspace scoping is a useful foundation; dedicated shared-access security tests and public-indexing guarantees required.

## Этап 5 — мобильный осмотр — осталось 7

- **89. Режим «Осмотр» — D.** `FIELD_INSPECTION_OPTIONS` is an enum only; no mobile GPS+polygon+distance route/data/test.
- **90. Выездной checklist — P.** `inspection_json`, статус выезда, несколько флагов и manual evidence upload существуют; полный мобильный checklist дороги/рельефа/воды/ЛЭП/мусора/шума/запаха отсутствует.
- **91. Фото с геопривязкой — D.** no geotagged photo metadata/storage/route.
- **92. Точки на карте — D.** no inspection map point entity/route.
- **93. Голосовые заметки — D.** no voice upload/transcription/linkage task.
- **94. Отчёт осмотра — D.** no inspection report vertical.
- **95. Сравнение с автоматикой — D.** no automated-vs-field discrepancy comparison.

## Этап 6 — после победы, продажа и данные — осталось 21

- **96. Режим «Я выиграл» — P.** pipeline has won/contract/rights states; no dedicated “I won” transition workflow.
- **97. Checklist после победы — D.** no post-win checklist with protocol/contract/payments/registration evidence.
- **98. Дедлайны — D.** no date-derived post-win deadline engine/reminders.
- **99. Финансовая карточка покупки — P.** fields/adapters for actual costs exist; no user-facing purchase financial card.
- **100. Учёт расходов — P.** actual-cost writer/adapters/tests exist; complete confirmed-expense ledger UX is absent.
- **101. Фактическая себестоимость — P.** `auction_actual_cost_*` and models support calculation; no complete purchase+all-linked-cost acceptance path.
- **102. Продажа участка — D.** no listing/lead/price-change/sale-date workflow.
- **103. Чистая прибыль — D.** no net-profit calculation from realized sale and taxes/expenses.
- **104. ROI и срок владения — D.** no ROI/holding-period report.
- **105. Анализ ошибок — D.** no hypothesis-vs-STOP-vs-actual outcome analysis.
- **106. Портфель пользователя — P.** portfolio route/template and pipeline states exist; purchased/for-sale/sold financial result coverage is not proven.
- **107. Добровольный вклад в базу — N.** no explicit consent-to-anonymize-and-contribute workflow.
- **108. Обезличивание — N.** no verified anonymization pipeline removing personal/private identifiers before reuse.
- **109. Мотивация за вклад — D.** incentives explicitly later; no implementation evidence.
- **110. Каталог экспертов — D.** no expert catalog entity/search/route.
- **111. Платная экспертная проверка — D.** no paid expert order/payment/delivery flow reusing DD.
- **112. Экспорт отчёта — P.** dossier text and admin CSV exist; requested PDF report with maps/legal/docs/market/STOP is not implemented.
- **113. API/архитектура данных — P.** code separates auction/history/evidence/documents/spatial/decision modules, but target `system_checks`, `user_due_diligence`, `shared_access` and complete data contract are not delivered.
- **114. Аудит действий — P.** activity/change/snapshot audit foundations exist; critical-data and consent audit is incomplete.
- **115. Безопасность файлов — P.** storage path scoping, private workspace and download checks exist; signed/revocable links, malware/content controls and negative ACL tests are not proven.
- **116. Критерий качества продукта — P.** unknown/manual_required labels and rule-vs-LLM boundary exist; release must prove unknown preservation end-to-end, not only display labels.
