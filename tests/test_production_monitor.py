import httpx

from tools.production_monitor import MonitorResult, check_application


class StubClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = iter(responses)

    def get(self, _url: str) -> httpx.Response:
        return next(self.responses)


def test_monitor_accepts_healthy_and_ready_endpoints() -> None:
    client = StubClient(
        [
            httpx.Response(200, json={"status": "ok"}),
            httpx.Response(200, json={"status": "ready"}),
        ]
    )

    assert check_application(client, "http://web:8000") == MonitorResult(
        True, "health and readiness checks passed"
    )


def test_monitor_reports_readiness_failure() -> None:
    client = StubClient(
        [
            httpx.Response(200, json={"status": "ok"}),
            httpx.Response(503, json={"detail": "not ready"}),
        ]
    )

    result = check_application(client, "http://web:8000/")

    assert result.ok is False
    assert result.detail == "/ready returned HTTP 503"
