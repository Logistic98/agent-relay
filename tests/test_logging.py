import json
import logging

from core.logging import JsonFormatter, configure_logging


def test_json_formatter_redacts_message_and_avoids_traceback_body() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="token=secret-value",
        args=(),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "token=[REDACTED]"
    assert payload["level"] == "ERROR"


def test_configure_logging_suppresses_http_client_info_urls() -> None:
    configure_logging("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
