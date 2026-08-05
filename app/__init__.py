"""Land Scout application package."""

import logging
import re

_TELEGRAM_TOKEN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")


class TelegramTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TELEGRAM_TOKEN.sub("<telegram-token-redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_safe_logging() -> None:
    token_filter = TelegramTokenFilter()
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        logger.addFilter(token_filter)
    for name in ("aiogram", "app.bot", "app.services"):
        logging.getLogger(name).addFilter(token_filter)


configure_safe_logging()
