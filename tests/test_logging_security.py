import logging

from app import TelegramTokenFilter


def test_http_client_info_logging_is_disabled() -> None:
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_telegram_token_is_redacted_from_log_record() -> None:
    token = "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    record = logging.LogRecord(
        name="httpx",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=f'POST https://api.telegram.org/bot{token}/sendMessage',
        args=(),
        exc_info=None,
    )

    assert TelegramTokenFilter().filter(record) is True
    assert token not in record.getMessage()
    assert "<telegram-token-redacted>" in record.getMessage()
