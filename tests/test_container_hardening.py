from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_image_uses_non_root_user_and_writable_document_cache() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "adduser --system --ingroup app app" in dockerfile
    assert "chown -R app:app /app/var" in dockerfile
    assert "USER app" in dockerfile
    assert "/app/var/auction-documents" in dockerfile


def test_web_service_has_runtime_healthcheck() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    web_section = compose.split("  web:\n", 1)[1].split("  worker:\n", 1)[0]
    assert "healthcheck:" in web_section
    assert "http://127.0.0.1:8000/ready" in web_section
    assert '@app.get("/ready")' in main_source
    assert 'connection.execute(text("SELECT 1"))' in main_source
    assert "client.ping()" in main_source


def test_beat_schedule_uses_runtime_writable_directory() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    beat_section = compose.split("  beat:\n", 1)[1].split("  bot:\n", 1)[0]
    assert "--schedule /app/var/celerybeat-schedule" in beat_section
