# server/log_handler.py
import logging
import time
from server import state as shared


class BufferHandler(logging.Handler):
    LEVEL_COLORS = {
        "DEBUG":    "#6c757d",
        "INFO":     "#4f98a3",
        "WARNING":  "#fdab43",
        "ERROR":    "#dd6974",
        "CRITICAL": "#a12c7b",
    }

    def emit(self, record: logging.LogRecord):
        try:
            shared.log_buffer.append({
                "ts":      time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level":   record.levelname,
                "color":   self.LEVEL_COLORS.get(record.levelname, "#cdccca"),
                "logger":  record.name,
                "message": self.format(record),
            })
        except Exception:
            pass
