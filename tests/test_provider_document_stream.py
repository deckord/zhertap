from __future__ import annotations

import gzip

import httpx
import pytest

from app.auction_v2 import _bounded_document_response


def test_document_stream_rejects_declared_oversize_before_body_buffering() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "1000000"},
            content=b"body is deliberately irrelevant",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="document is larger"):
            _bounded_document_response(client, "https://example.test/document", max_bytes=16)


def test_document_stream_stops_when_chunked_body_crosses_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"0123456789abcdef", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="document is larger"):
            _bounded_document_response(client, "https://example.test/document", max_bytes=8)


def test_document_stream_does_not_double_decode_encoded_body() -> None:
    original = b"%PDF-1.7\ncompressed body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(gzip.compress(original))),
            },
            content=gzip.compress(original),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = _bounded_document_response(
            client,
            "https://example.test/document",
            max_bytes=1024,
        )

    assert response.content == original
    assert "Content-Encoding" not in response.headers
    assert response.headers["Content-Length"] == str(len(original))
