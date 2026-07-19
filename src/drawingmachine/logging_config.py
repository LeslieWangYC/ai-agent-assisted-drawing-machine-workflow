import logging
import sys

from drawingmachine.config import LogLevel

_LOGGER_NAME = "drawingmachine"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: LogLevel) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level.value)
    logger.propagate = False
