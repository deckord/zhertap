from tools.local_db_concurrency_probe import run_probe


def test_local_db_concurrency_probe_smoke() -> None:
    result = run_probe(total=20, workers=5, pool_size=3, max_overflow=2)

    assert result["requests"] == 20
    assert result["accounts"] == 20
