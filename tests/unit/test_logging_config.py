import io
import logging

import pytest

from drawingmachine.config import LogLevel
from drawingmachine.logging_config import configure_logging


@pytest.fixture(autouse=True)
def restore_drawingmachine_logger() -> None:
    logger = logging.getLogger("drawingmachine")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    try:
        yield
    finally:
        for handler in logger.handlers:
            if handler not in original_handlers:
                handler.close()
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_logging_uses_stderr_without_secret_values(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAWINGMACHINE_TEST_SECRET", "do-not-log-this-value")

    configure_logging(LogLevel.INFO)
    logging.getLogger("drawingmachine").info("service ready")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert " INFO drawingmachine service ready" in captured.err
    assert "do-not-log-this-value" not in captured.err


def test_logging_replaces_only_drawingmachine_handlers() -> None:
    root = logging.getLogger()
    root_handler = logging.StreamHandler(io.StringIO())
    root.addHandler(root_handler)
    package_logger = logging.getLogger("drawingmachine")
    old_package_handler = logging.StreamHandler(io.StringIO())
    package_logger.addHandler(old_package_handler)
    try:
        configure_logging(LogLevel.WARNING)

        assert root_handler in root.handlers
        assert old_package_handler not in package_logger.handlers
        assert len(package_logger.handlers) == 1
        assert package_logger.level == logging.WARNING
        assert package_logger.propagate is False
    finally:
        root.removeHandler(root_handler)
        root_handler.close()
        old_package_handler.close()
