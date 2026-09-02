import pytest

import app.email_delivery as email_delivery
from app.config import settings


class SMTPStub:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args = None
        self.message = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        return None

    def starttls(self, *, context):
        self.starttls_called = context is not None

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.message = message


def test_password_reset_email_uses_authenticated_starttls_smtp(monkeypatch) -> None:
    SMTPStub.instances.clear()
    monkeypatch.setattr(settings, "smtp_enabled", True)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.kz")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "mailer")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "smtp_from_email", "no-reply@zhertap.kz")
    monkeypatch.setattr(settings, "smtp_starttls", True)
    monkeypatch.setattr(settings, "smtp_timeout_seconds", 15)
    monkeypatch.setattr(settings, "password_reset_token_minutes", 30)
    monkeypatch.setattr(email_delivery.smtplib, "SMTP", SMTPStub)

    email_delivery.send_password_reset_email(
        "client@example.kz",
        "https://zhertap.kz/password/reset/email/token-value",
    )

    smtp = SMTPStub.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.kz", 587, 15)
    assert smtp.starttls_called is True
    assert smtp.login_args == ("mailer", "secret")
    assert smtp.message["From"] == "no-reply@zhertap.kz"
    assert smtp.message["To"] == "client@example.kz"
    assert "token-value" in smtp.message.get_content()
    assert "30 минут" in smtp.message.get_content()


def test_password_reset_email_refuses_disabled_smtp(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_enabled", False)

    with pytest.raises(RuntimeError, match="SMTP is disabled"):
        email_delivery.send_password_reset_email(
            "client@example.kz",
            "https://zhertap.kz/password/reset/email/token-value",
        )


def test_password_reset_email_supports_mail_ru_implicit_ssl(monkeypatch) -> None:
    class SMTPSSLStub(SMTPStub):
        def __init__(self, host, port, timeout, context):
            super().__init__(host, port, timeout)
            self.ssl_context = context

    SMTPSSLStub.instances.clear()
    monkeypatch.setattr(settings, "smtp_enabled", True)
    monkeypatch.setattr(settings, "smtp_host", "smtp.mail.ru")
    monkeypatch.setattr(settings, "smtp_port", 465)
    monkeypatch.setattr(settings, "smtp_username", "no-reply@zhertap.kz")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "smtp_from_email", "no-reply@zhertap.kz")
    monkeypatch.setattr(settings, "smtp_ssl", True)
    monkeypatch.setattr(settings, "smtp_starttls", True)
    monkeypatch.setattr(email_delivery.smtplib, "SMTP_SSL", SMTPSSLStub)

    email_delivery.send_password_reset_email(
        "client@example.kz",
        "https://zhertap.kz/password/reset/email/token-value",
    )

    smtp = SMTPSSLStub.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.mail.ru", 465)
    assert smtp.ssl_context is not None
    assert smtp.starttls_called is False
    assert smtp.login_args == ("no-reply@zhertap.kz", "secret")
