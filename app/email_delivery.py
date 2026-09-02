from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from app.config import settings


def send_password_reset_email(email: str, reset_url: str) -> None:
    if not settings.smtp_enabled:
        raise RuntimeError("SMTP is disabled")
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP host and sender are not configured")
    if settings.smtp_username and not settings.smtp_password:
        raise RuntimeError("SMTP password is not configured")

    message = EmailMessage()
    message["Subject"] = "Восстановление пароля Жертап"
    message["From"] = settings.smtp_from_email
    message["To"] = email
    message.set_content(
        "Вы запросили восстановление пароля Жертап.\n\n"
        f"Откройте ссылку: {reset_url}\n\n"
        f"Ссылка действует {settings.password_reset_token_minutes} минут "
        "и может быть использована только один раз.\n\n"
        "Если вы не запрашивали восстановление, проигнорируйте это письмо."
    )

    try:
        if settings.smtp_ssl:
            smtp_client = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            smtp_client = smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            )
        with smtp_client as client:
            client.ehlo()
            if settings.smtp_starttls and not settings.smtp_ssl:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise RuntimeError("Could not send password reset email") from exc
