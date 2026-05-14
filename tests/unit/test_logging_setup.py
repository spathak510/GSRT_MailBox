from __future__ import annotations

import logging

from app.settings.logging import setup_logging


def test_setup_logging_writes_to_file(tmp_path) -> None:
    root_logger = logging.getLogger()
    log_path = tmp_path / "app.log"

    setup_logging("INFO", log_path)
    logging.getLogger("tests.logging").info("logging smoke test")

    for handler in root_logger.handlers:
        handler.flush()

    assert log_path.exists()
    assert "logging smoke test" in log_path.read_text(encoding="utf-8")
    assert any(
        getattr(handler, "baseFilename", None) == str(log_path)
        for handler in root_logger.handlers
    )