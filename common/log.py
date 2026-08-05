import logging
import os
import sys
import io


_LOG_FORMAT = "[%(levelname)s][%(asctime)s][%(filename)s:%(lineno)d] - %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _log_path():
    # Mirror config.get_data_root() without importing config (avoids a circular
    # import, since config imports this module). The desktop build sets
    # COW_DATA_DIR (e.g. ~/.cow); source deployments fall back to CWD.
    data_dir = os.environ.get("COW_DATA_DIR")
    if data_dir:
        data_dir = os.path.expanduser(data_dir)
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "run.log")
    return "run.log"


def _make_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)


def _try_add_file_handler(log: logging.Logger) -> bool:
    """Attach a FileHandler writing to run.log. Returns False if unwritable."""
    try:
        file_handle = logging.FileHandler(_log_path(), encoding="utf-8")
        file_handle.setFormatter(_make_formatter())
        log.addHandler(file_handle)
        return True
    except OSError:
        return False


def _clear_handlers(log: logging.Logger) -> None:
    for handler in list(log.handlers):
        handler.close()
        log.removeHandler(handler)
    log.handlers.clear()


def _reset_logger(log):
    _clear_handlers(log)
    log.propagate = False
    stdout = sys.stdout
    if hasattr(stdout, "buffer"):
        stdout = io.TextIOWrapper(stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    console_handle = logging.StreamHandler(stdout)
    console_handle.setFormatter(_make_formatter())
    log.addHandler(console_handle)
    # File logging is best-effort: if the log path isn't writable (e.g. a
    # packaged app installed under Program Files run by a non-admin user, with
    # an unwritable CWD), fall back to console-only instead of crashing the
    # whole process at import time.
    if not _try_add_file_handler(log):
        console_handle.handle(
            logging.LogRecord(
                "log", logging.WARNING, __file__, 0,
                "[log] file logging disabled (log path not writable): %s",
                (_log_path(),), None,
            )
        )


def _get_logger():
    log = logging.getLogger("log")
    _reset_logger(log)
    log.setLevel(logging.INFO)
    return log


def _get_file_logger():
    """Logger that writes only to run.log (no console).

    Used for high-frequency session-scan diagnostics so the terminal stays
    readable while full detail is still available in the log file.
    """
    log = logging.getLogger("log.file_only")
    _clear_handlers(log)
    log.propagate = False
    log.setLevel(logging.INFO)
    if not _try_add_file_handler(log):
        # Prefer silence over re-flooding the console when the log path is bad.
        log.addHandler(logging.NullHandler())
    return log


# 日志句柄：控制台 + 文件
logger = _get_logger()
# 仅文件：会话扫描等高频诊断日志
file_logger = _get_file_logger()
