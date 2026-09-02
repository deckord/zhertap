from types import SimpleNamespace

from app.auction_v2 import _match_refreshed_document_url


def test_document_url_refresh_matches_title_and_returns_fresh_signed_url() -> None:
    document = SimpleNamespace(title="22.07 пост.pdf")
    refreshed = [
        SimpleNamespace(title="Другой.pdf", source_url="https://example.test/a"),
        SimpleNamespace(title="22.07 пост.pdf", source_url="https://example.test/fresh.pdf?Token=new"),
    ]

    assert _match_refreshed_document_url(document, refreshed) == (
        "https://example.test/fresh.pdf?Token=new"
    )


def test_document_url_refresh_does_not_match_different_title() -> None:
    document = SimpleNamespace(title="Ответ.pdf")
    refreshed = [SimpleNamespace(title="Другой.pdf", source_url="https://example.test/a")]

    assert _match_refreshed_document_url(document, refreshed) is None
