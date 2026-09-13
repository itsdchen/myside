import logging
from logging.handlers import RotatingFileHandler
from logging import handlers
import sys


def getLogger(log_name, file_name):
    log = logging.getLogger(log_name)
    log.setLevel(logging.DEBUG)

    format = logging.Formatter("%(asctime)s - %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(format)
    log.addHandler(ch)

    fh = handlers.RotatingFileHandler(file_name, maxBytes=(1048576*5), backupCount=7)
    fh.setFormatter(format)
    log.addHandler(fh)
    return log
