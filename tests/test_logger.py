import logging

from utils.logger import setup_logger


def _file_handler(logger):
    return next(
        handler
        for handler in logger.handlers
        if isinstance(handler, logging.FileHandler)
    )


def test_file_handler_respects_default_info_level(tmp_path):
    logger = setup_logger(str(tmp_path), log_level="INFO")
    try:
        handler = _file_handler(logger)
        logger.debug("debug-private-payload")
        logger.info("safe-info")
        handler.flush()

        text = next(tmp_path.glob("kol_workflow_*.log")).read_text(encoding="utf-8")
        assert "safe-info" in text
        assert "debug-private-payload" not in text
        assert handler.level == logging.INFO
    finally:
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()


def test_file_handler_keeps_safe_debug_diagnostics_when_requested(tmp_path):
    logger = setup_logger(str(tmp_path), log_level="DEBUG")
    try:
        handler = _file_handler(logger)
        logger.debug("batch=1 count=2 fields=['Name'] message=<redacted>")
        handler.flush()

        text = next(tmp_path.glob("kol_workflow_*.log")).read_text(encoding="utf-8")
        assert "batch=1 count=2 fields=['Name'] message=<redacted>" in text
        assert handler.level == logging.DEBUG
    finally:
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()
