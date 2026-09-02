# Аудит нового ТЗ: единая карточка аукционного участка

Дата: 2026-08-28

Статусы: **I** — реализовано и подтверждено; **P** — частично / зависит от данных; **D** — фундамент или ручной процесс; **N** — отсутствует.

| № | Требование | Статус | Подтверждённое состояние / главный пробел |
|---|---|---|---|
| 1 | Единый объект E-Qazyna ↔ Jerler ↔ ЕГКН | P | В `AuctionLot` есть `source_lot_id`, кадастр, `land_object_id`; canonical link-table и tri-source merge отсутствуют. В БД `land_object_id` заполнен у 1 из 3325. |
| 2 | Автопроверка ЕГКН | P | Есть `EgknProvider`, WFS и `auction_lot_geo_checks`; geo-check только у 587/3325 лотов. |
| 3 | Cross-source расхождения | P | Jerler conflict evidence предусмотрен, но полноценного reconciliation и операторского resolution нет; evidence Jerler в production нет. |
| 4 | Генплан | P | Детерминированный planning-context есть, но production spatial-feed states/manifests пусты. |
| 5 | ПДП, красные линии, будущие дороги | P | Правила и adapters есть; фактически GIS-feed не наполнен. |
| 6 | GIS-пересечения и расстояния | P | Геометрия/ограничения/OSM реализованы, но нет доказанного полного spatial coverage. |
| 7 | Соседние кадастровые участки | N | Есть аналогичная LPH-логика, но нет auction-DD анализа соседей и структуры территории. |
| 8 | Структура окружения ЕГКН | N | Нет агрегирования ИЖС/коммерция/туризм/ЛПХ вокруг polygon аукционного лота. |
| 9 | Document Intelligence | P | PDF/DOCX/OCR/extraction/conflicts есть; ready 528 из 10057 states, pending 8434. |
| 10 | Red flags | P | Fail-closed verdict/risk rules есть; отдельного реестра red flags и наполненных GIS identity flags нет. |
| 11 | История объекта | P | Land object/cadastre timeline и normalization есть; полнота архива не доказана. |
| 12 | История результатов торгов | P | Статусы/цены/даты хранятся; нет полного подтверждённого протокольного архива для каждого лота. |
| 13 | Аналоги аукционов | P | Строгая логика медиан и comparable rules есть; verified market inventory в production пуст. |
| 14 | Вторичный рынок Krisha/OLX | D | Разделение «объявления ≠ сделки» и ручной ввод есть; автоматического ingest нет, таблица market comparables пуста. |
| 15 | Позитивные инвестсобытия | N | Нет TerritoryEvent/InvestmentEvent моделей, провайдеров и геопривязки. |
| 16 | Негативные события | P | Есть OSM/GIS факторы (кладбища, свалки, промзоны); нет временного реестра негативных событий. |
| 17 | Статусы проектов | N | Есть пользовательский lifecycle лота, но нет lifecycle территориальных событий. |
| 18 | Расстояние до инфраструктуры | P | OSM расстояния и геометрия есть; не применяются к инвест/негативным событиям, потому что их нет. |
| 19 | Демография | N | Нет демографических источников, моделей, агрегации и тренда. |
| 20 | Структурированный итог | P | Есть `data_quality`, decision summary, risks; нет единого Territory Intelligence contract. |
| 21 | Что нельзя проверить | I | Fail-closed unknown/unresolved факты и UI-блок «Не подтверждено» есть; пока не покрывает отсутствующие territory/demography домены. |
| 22 | Непредписывающий вывод | N | Текущий verdict содержит `participate/do_not_participate`; нужно заменить на нейтральный формат факторов, рисков и неизвестного. |

## Production риски, которые блокируют заявление «полная автоматическая DD»

1. GIS spatial-feed pipeline пуст: planning/PDP/red-line/zone checks не имеют production данных.
2. У идентичности участка нет заполненного canonical object layer.
3. Document extraction backlog: большинство документов ещё не извлечено.
4. Verified auction comparable/secondary-market inventory пуст.
5. Territory events и демография отсутствуют полностью.
6. Migration graph и dirty production checkout несогласованы; перед расширением требуется нормализовать миграции и release state.

## Порядок внедрения

1. Стабилизировать release/migration graph и source freshness.
2. Ввести canonical LandObject + links E-Qazyna/Jerler/EGKN и конфликтный reconciliation workflow.
3. Наполнить/запустить spatial feeds и polygon checks; затем соседние кадастровые участки.
4. Завершить document extraction и evidence/provenance UI.
5. Довести историю и verified comparables; вторичный рынок — отдельный ingestion с маркировкой offer price.
6. Создать Territory Event registry, статусный lifecycle, geo-distance и только потом демографию.
7. Заменить предписывающий verdict нейтральным evidence-backed итогом.
