from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import exists, func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.auction_history_normalization import (
    BackfillCheckpoint,
    HistoryGenerationRun,
    NormalizedAuctionHistoryRow,
    RawAuctionHistoryRecord,
)
from app.models import (
    AuctionHistoryGeneration,
    AuctionHistoryGenerationLot,
    AuctionHistoryNormalized,
    AuctionLot,
)

GENERATION_LOCK_KEY = "auction-history-normalization-generation"


class SqlAlchemyHistoryNormalizationStore:
    """Short-transaction persistence adapter for normalized auction history."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _advisory_lock(self) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": GENERATION_LOCK_KEY},
            )

    @staticmethod
    def _to_run(model: AuctionHistoryGeneration) -> HistoryGenerationRun:
        cutoff = model.source_cutoff
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        return HistoryGenerationRun(
            generation=model.generation,
            status=model.status,
            source_cutoff=cutoff,
            source_high_water_lot_id=model.source_high_water_lot_id,
            expected_count=model.expected_count,
            processed_count=model.processed_count,
            error_count=model.error_count,
            checkpoint=BackfillCheckpoint(model.checkpoint_lot_id),
            scan_complete=model.scan_complete,
            detail=model.detail,
            normalization_version=model.normalization_version,
        )

    @staticmethod
    def _snapshot_conditions(source_cutoff: datetime) -> tuple[object, ...]:
        # created_at is immutable: a lot updated while a generation is scanning must
        # remain inside the snapshot. Filtering by updated_at makes rows disappear
        # between batches and leaves the generation permanently unreconciled.
        return (
            AuctionLot.object_type == "land",
            AuctionLot.created_at <= source_cutoff,
        )

    def create_building_generation(
        self,
        source_cutoff: datetime,
        normalization_version: str,
    ) -> HistoryGenerationRun | None:
        with self.session.begin():
            self._advisory_lock()
            existing = self.session.scalar(
                select(AuctionHistoryGeneration)
                .where(AuctionHistoryGeneration.status == "building")
                .with_for_update()
                .limit(1)
            )
            if existing is not None:
                return None
            next_generation = (
                self.session.scalar(select(func.max(AuctionHistoryGeneration.generation))) or 0
            ) + 1
            now = datetime.now(UTC)
            model = AuctionHistoryGeneration(
                generation=next_generation,
                normalization_version=normalization_version,
                status="building",
                source_cutoff=source_cutoff,
                source_high_water_lot_id=None,
                expected_count=0,
                processed_count=0,
                error_count=0,
                checkpoint_lot_id=None,
                scan_complete=False,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            self.session.add(model)
            self.session.flush()
            self.session.execute(
                insert(AuctionHistoryGenerationLot).from_select(
                    ["generation", "lot_id"],
                    select(literal(next_generation), AuctionLot.id).where(
                        *self._snapshot_conditions(source_cutoff)
                    ),
                )
            )
            high_water, expected_count = self.session.execute(
                select(
                    func.max(AuctionHistoryGenerationLot.lot_id),
                    func.count(AuctionHistoryGenerationLot.lot_id),
                ).where(AuctionHistoryGenerationLot.generation == next_generation)
            ).one()
            model.source_high_water_lot_id = high_water
            model.expected_count = int(expected_count or 0)
            self.session.flush()
            return self._to_run(model)

    def fetch_snapshot_after(
        self,
        run: HistoryGenerationRun,
        limit: int,
    ) -> list[RawAuctionHistoryRecord]:
        if run.expected_count == 0:
            return []
        conditions = [
            AuctionHistoryGenerationLot.generation == run.generation,
        ]
        if run.checkpoint.after_lot_id is not None:
            conditions.append(AuctionHistoryGenerationLot.lot_id > run.checkpoint.after_lot_id)
        statement = (
            select(
                AuctionLot.id,
                AuctionLot.updated_at,
                AuctionLot.status,
                AuctionLot.source_search_status,
                AuctionLot.land_rights,
                AuctionLot.purpose,
                AuctionLot.title,
                AuctionLot.use_goal,
                AuctionLot.functional_purpose_level2,
                AuctionLot.functional_purpose_level3,
                AuctionLot.functional_purpose_level4,
                AuctionLot.lease_term_years,
                AuctionLot.auction_starts_at,
                AuctionLot.published_at,
                AuctionLot.area_ha,
                AuctionLot.start_price_kzt,
                AuctionLot.sale_price_kzt,
                AuctionLot.region,
                AuctionLot.district,
                AuctionLot.locality,
            )
            .join(
                AuctionHistoryGenerationLot,
                AuctionHistoryGenerationLot.lot_id == AuctionLot.id,
            )
            .where(*conditions)
            .order_by(AuctionHistoryGenerationLot.lot_id.asc())
            .limit(limit)
        )
        with self.session.begin():
            rows = self.session.execute(statement).all()
        return [
            RawAuctionHistoryRecord(
                lot_id=row.id,
                source_updated_at=self._aware(row.updated_at),
                status=row.status,
                source_search_status=row.source_search_status,
                land_rights=row.land_rights,
                purpose=row.purpose,
                title=row.title,
                use_goal=row.use_goal,
                functional_purpose=row.functional_purpose_level4
                or row.functional_purpose_level3
                or row.functional_purpose_level2,
                purpose_claims=tuple(
                    value
                    for value in (
                        row.functional_purpose_level2,
                        row.functional_purpose_level3,
                        row.functional_purpose_level4,
                    )
                    if value
                ),
                lease_term_years=row.lease_term_years,
                auction_starts_at=self._aware(row.auction_starts_at),
                published_at=row.published_at,
                area_ha=row.area_ha,
                start_price_kzt=row.start_price_kzt,
                sale_price_kzt=row.sale_price_kzt,
                region=row.region,
                district=row.district,
                locality=row.locality,
            )
            for row in rows
        ]

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @staticmethod
    def _decimal(value: float | None) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None

    @classmethod
    def _row_payload(cls, row: NormalizedAuctionHistoryRow) -> dict[str, object]:
        return {
            "generation": row.generation,
            "lot_id": row.lot_id,
            "normalization_version": row.normalization_version,
            "normalization_key": row.normalization_key,
            "right_kind": row.right_kind,
            "right_status": row.right_status,
            "purpose_group": row.purpose_group,
            "purpose_status": row.purpose_status,
            "lease_band": row.lease_band,
            "lease_status": row.lease_status,
            "event_date": row.event_date,
            "event_date_status": row.event_date_status,
            "outcome": row.outcome,
            "outcome_status": row.outcome_status,
            "area_ha": cls._decimal(row.area_ha),
            "area_status": row.area_status,
            "start_price_kzt": cls._decimal(row.start_price_kzt),
            "start_price_status": row.start_price_status,
            "sale_price_kzt": cls._decimal(row.sale_price_kzt),
            "sale_price_status": row.sale_price_status,
            "sale_to_start_ratio": cls._decimal(row.sale_to_start_ratio),
            "start_price_per_ha_kzt": cls._decimal(row.start_price_per_ha_kzt),
            "sale_price_per_ha_kzt": cls._decimal(row.sale_price_per_ha_kzt),
            "region_key": row.region_key,
            "district_key": row.district_key,
            "locality_key": row.locality_key,
            "source_updated_at": row.source_updated_at,
            "issues_json": json.dumps(row.issues, ensure_ascii=False, separators=(",", ":")),
            "normalized_at": datetime.now(UTC),
        }

    def _upsert_rows(self, rows: list[NormalizedAuctionHistoryRow]) -> int:
        if not rows:
            return 0
        payloads = [self._row_payload(row) for row in rows]
        table = AuctionHistoryNormalized.__table__
        dialect = self.session.get_bind().dialect.name
        insert_factory = postgresql_insert if dialect == "postgresql" else sqlite_insert
        statement = insert_factory(table).values(payloads)
        excluded = statement.excluded
        update_values = {
            column.name: getattr(excluded, column.name)
            for column in table.columns
            if column.name not in {"generation", "lot_id"}
        }
        statement = statement.on_conflict_do_update(
            index_elements=["generation", "lot_id"],
            set_=update_values,
            where=table.c.normalization_key != excluded.normalization_key,
        )
        result = self.session.execute(statement)
        return max(0, min(int(result.rowcount or 0), len(rows)))

    def _locked_generation(self, generation: int) -> AuctionHistoryGeneration | None:
        return self.session.scalar(
            select(AuctionHistoryGeneration)
            .where(AuctionHistoryGeneration.generation == generation)
            .with_for_update()
            .limit(1)
        )

    def commit_batch(
        self,
        run: HistoryGenerationRun,
        rows: list[NormalizedAuctionHistoryRow],
        next_checkpoint: BackfillCheckpoint,
        scan_complete: bool,
    ) -> tuple[HistoryGenerationRun, int]:
        with self.session.begin():
            model = self._locked_generation(run.generation)
            if model is None:
                raise RuntimeError("generation disappeared")
            if model.status != "building" or model.checkpoint_lot_id != run.checkpoint.after_lot_id:
                return self._to_run(model), 0
            upserted = self._upsert_rows(rows)
            model.processed_count += len(rows)
            model.checkpoint_lot_id = next_checkpoint.after_lot_id
            model.scan_complete = scan_complete
            model.updated_at = datetime.now(UTC)
            self.session.flush()
            return self._to_run(model), upserted

    def fail_generation(
        self,
        run: HistoryGenerationRun,
        detail: str,
    ) -> HistoryGenerationRun:
        with self.session.begin():
            model = self._locked_generation(run.generation)
            if model is None:
                raise RuntimeError("generation disappeared")
            if model.status == "building":
                model.status = "failed"
                model.error_count += 1
                model.detail = detail[:1000]
                model.completed_at = datetime.now(UTC)
                model.updated_at = datetime.now(UTC)
                self.session.flush()
            return self._to_run(model)

    def reconcile_and_promote(self, run: HistoryGenerationRun) -> HistoryGenerationRun:
        with self.session.begin():
            self._advisory_lock()
            model = self._locked_generation(run.generation)
            if model is None:
                raise RuntimeError("generation disappeared")
            normalized_count = self.session.scalar(
                select(func.count(AuctionHistoryNormalized.lot_id)).where(
                    AuctionHistoryNormalized.generation == run.generation
                )
            ) or 0
            membership_count = self.session.scalar(
                select(func.count(AuctionHistoryGenerationLot.lot_id)).where(
                    AuctionHistoryGenerationLot.generation == run.generation
                )
            ) or 0
            missing_normalized = self.session.scalar(
                select(
                    exists().where(
                        AuctionHistoryGenerationLot.generation == run.generation,
                        ~exists().where(
                            AuctionHistoryNormalized.generation == run.generation,
                            AuctionHistoryNormalized.lot_id == AuctionHistoryGenerationLot.lot_id,
                        ),
                    )
                )
            )
            extra_normalized = self.session.scalar(
                select(
                    exists().where(
                        AuctionHistoryNormalized.generation == run.generation,
                        ~exists().where(
                            AuctionHistoryGenerationLot.generation == run.generation,
                            AuctionHistoryGenerationLot.lot_id == AuctionHistoryNormalized.lot_id,
                        ),
                    )
                )
            )
            reconciled = (
                model.status == "building"
                and model.scan_complete
                and model.error_count == 0
                and model.expected_count
                == model.processed_count
                == int(normalized_count)
                == int(membership_count)
                and not missing_normalized
                and not extra_normalized
            )
            now = datetime.now(UTC)
            if not reconciled:
                if model.status != "building":
                    return self._to_run(model)
                model.status = "failed"
                model.error_count += 1
                model.detail = "generation reconciliation failed"
                model.completed_at = now
                model.updated_at = now
                self.session.flush()
                return self._to_run(model)
            self.session.execute(
                update(AuctionHistoryGeneration)
                .where(
                    AuctionHistoryGeneration.status == "active",
                    AuctionHistoryGeneration.generation != model.generation,
                )
                .values(status="superseded", updated_at=now)
            )
            self.session.flush()
            model.status = "active"
            model.activated_at = now
            model.completed_at = now
            model.updated_at = now
            self.session.flush()
            return self._to_run(model)

    def get_active_generation(self) -> HistoryGenerationRun | None:
        model = self.session.scalar(
            select(AuctionHistoryGeneration)
            .where(AuctionHistoryGeneration.status == "active")
            .limit(1)
        )
        return self._to_run(model) if model is not None else None

    def get_building_generation(self) -> HistoryGenerationRun | None:
        model = self.session.scalar(
            select(AuctionHistoryGeneration)
            .where(AuctionHistoryGeneration.status == "building")
            .limit(1)
        )
        return self._to_run(model) if model is not None else None

    def get_generation(self, generation: int) -> HistoryGenerationRun | None:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return None
        model = self.session.get(AuctionHistoryGeneration, generation)
        return self._to_run(model) if model is not None else None
