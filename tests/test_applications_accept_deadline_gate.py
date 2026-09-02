from datetime import UTC, datetime, timedelta

from test_auction_v2 import build_session, make_admin_account, make_lot

from app.auction_service import AuctionFilters
from app.auction_v2 import AuctionV2Filters, list_auction_v2_lots


def test_applications_accept_filter_excludes_lot_after_auction_start() -> None:
    with build_session() as session:
        account = make_admin_account()
        open_lot = make_lot()
        open_lot.source_lot_id = "open"
        open_lot.source_search_status = "ApplicationsAccept"
        open_lot.auction_starts_at = datetime.now(UTC) + timedelta(hours=1)
        closed_lot = make_lot()
        closed_lot.source_lot_id = "closed"
        closed_lot.source_search_status = "ApplicationsAccept"
        closed_lot.auction_starts_at = datetime.now(UTC) - timedelta(minutes=1)
        session.add_all([account, open_lot, closed_lot])
        session.commit()

        rows, total = list_auction_v2_lots(
            session,
            AuctionV2Filters(
                base=AuctionFilters(), eqazyna_status="ApplicationsAccept"
            ),
            account_id=account.id,
        )

        assert total == 1
        assert [item.lot.id for item in rows] == [open_lot.id]
