import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Small dependency-free JSON-lines formatter for recoverable local logs."""

    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key in ("status_code", "request"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = str(value)
        return json.dumps(payload, ensure_ascii=True)
