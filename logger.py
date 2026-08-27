"""logger.py — Coloured console + rotating file logger"""
import logging, os
from logging.handlers import RotatingFileHandler
from config import LOG_FILE, LOG_LEVEL

os.makedirs("logs", exist_ok=True)

try:
    import colorlog
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False


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

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s", "%Y-%m-%d %H:%M:%S"))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


log = get_logger()
