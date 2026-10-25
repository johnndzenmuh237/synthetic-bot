"""logger.py — Coloured console + rotating file logger (secrets are redacted)"""
import logging, os, re, sys
from logging.handlers import RotatingFileHandler
from config import LOG_FILE, LOG_LEVEL

_log_dir = os.path.dirname(LOG_FILE)
if _log_dir:
    os.makedirs(_log_dir, exist_ok=True)

# Windows consoles default to cp1252 and choke on non-ASCII characters.
# Force UTF-8 (replacing anything unprintable) so logging never errors out.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import colorlog
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False


class RedactSecrets(logging.Filter):
    """Never let API tokens or one-time passwords reach the console or log file."""
    _PATTERNS = [
        (re.compile(r"pat_[A-Za-z0-9_\-]{8,}"), "pat_***"),
        (re.compile(r"(?i)(otp=)[^&\s\"']+"), r"\1***"),
        (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{8,}"), r"\1***"),
    ]

    def filter(self, record):
        try:
            msg = record.getMessage()
            for rx, repl in self._PATTERNS:
                msg = rx.sub(repl, msg)
            record.msg, record.args = msg, ()
        except Exception:
            pass
        return True


def get_logger(name="SyntheticBot"):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    if HAS_COLOR:
        ch = colorlog.StreamHandler()
        ch.setFormatter(colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)-8s]%(reset)s %(message)s",
            datefmt="%H:%M:%S",
            log_colors={"DEBUG":"cyan","INFO":"green","WARNING":"yellow",
                        "ERROR":"red","CRITICAL":"bold_red"}))
    else:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(message)s", "%H:%M:%S"))

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s", "%Y-%m-%d %H:%M:%S"))

    for h in (ch, fh):
        h.addFilter(RedactSecrets())
        logger.addHandler(h)
    return logger


log = get_logger()
