import httpx

from app.auction_v2 import sync_auction_v2_documents
from app.models import AuctionDocument
from app.providers.eqazyna import AuctionDocumentData
from tests.test_auction_v2 import build_session, make_lot


def test_stale_eqazyna_url_refresh_downloads_pdf_in_same_pass(monkeypatch) -> None:
    old_url = (
        "https://sauda.e-qazyna.kz/ru/MnuFileStoreFileDownload?FileId=old&Token=expired"
    )
    fresh_url = "https://sauda.e-qazyna.kz/ru/MnuFileStoreFileDownload?FileId=fresh&Token=new"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == fresh_url:
            return httpx.Response(200, content=b"%PDF-1.4\nfresh")
        return httpx.Response(200, content=b"<!doctype html><title>access denied</title>")

    monkeypatch.setattr(
        "app.auction_v2.EqazynaProvider.lot_detail",
        lambda *_args, **_kwargs: type(
            "FreshLot",
            (),
            {
                "documents": [
                    AuctionDocumentData(
                        title="Извещение о проведении торгов",
                        source_url=fresh_url,
                        file_type="pdf",
                    )
                ]
            },
        )(),
    )
    with build_session() as session:
        lot = make_lot()
        session.add(lot)
        session.commit()
        document = lot.documents[0]
        document.title = "Извещение о проведении торгов"
        document.source_url = old_url
        session.commit()

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = sync_auction_v2_documents(
                session,
                document_ids=[document.id],
                limit=1,
                enabled=True,
                client=client,
            )

        stored = session.get(AuctionDocument, document.id)
        assert result.downloaded == 1
        assert result.errors == 0
        assert stored is not None
        assert stored.storage_status == "downloaded"
        assert stored.source_url == fresh_url
        assert calls == [old_url, fresh_url]
