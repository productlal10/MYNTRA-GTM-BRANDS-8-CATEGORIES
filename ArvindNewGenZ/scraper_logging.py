"""Structured scraper logging helpers for terminal, file logs, and live UI status."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from config import LOGS_DIR


SCRAPER_EVENT_LOG_PATH = LOGS_DIR / "scraper_events.jsonl"
SCRAPER_STATUS_PATH = LOGS_DIR / "scrape_status_latest.json"

_STD_LOG_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def generate_run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{secrets.token_hex(3)}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _daily_log_path() -> Path:
    return LOGS_DIR / f"scraper_{datetime.now().astimezone().strftime('%Y%m%d')}.log"


def format_duration_compact(seconds: float | int | None) -> str:
    total_seconds = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class StructuredScraperFormatter(logging.Formatter):
    """Readable console/file formatter with section/context fields."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        section = str(getattr(record, "section", "general") or "general").upper()
        message = record.getMessage()
        context_chunks = []
        for key in ("run_id", "brand", "page", "route", "status", "saved", "seen", "expected", "attempt", "elapsed_label", "products_per_minute"):
            value = getattr(record, key, None)
            if value in (None, "", []):
                continue
            context_chunks.append(f"{key}={value}")
        context = f" | {' | '.join(context_chunks)}" if context_chunks else ""
        return f"{timestamp} | {record.levelname:<7} | {section:<9} | {message}{context}"


def configure_scraper_logging(level_name: str = "INFO") -> None:
    """Configure terminal + file logging for scraper runs."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = StructuredScraperFormatter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(root_logger.level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOGS_DIR / "scraper.log", encoding="utf-8")
    file_handler.setLevel(root_logger.level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    daily_file_handler = logging.FileHandler(_daily_log_path(), encoding="utf-8")
    daily_file_handler.setLevel(root_logger.level)
    daily_file_handler.setFormatter(formatter)
    root_logger.addHandler(daily_file_handler)


class ScraperTelemetry:
    """Persists a live run status document plus structured JSONL events."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.event_log_path = SCRAPER_EVENT_LOG_PATH
        self.status_path = SCRAPER_STATUS_PATH
        self._lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "run_id": run_id,
            "status": "INITIALIZING",
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "current_brand": None,
            "config": {},
            "totals": {},
            "section_counts": {},
            "brands": {},
            "summary": {},
        }

    def start_run(self, config: Dict[str, Any]) -> None:
        with self._lock:
            self._status["status"] = "RUNNING"
            self._status["config"] = dict(config or {})
            self._status["updated_at"] = _now_iso()
            self._write_status_locked()
        self.record("INFO", "run", "Scraper run started.", config=config)

    def finish_run(self, status: str, summary: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._status["status"] = status
            self._status["finished_at"] = _now_iso()
            self._status["updated_at"] = self._status["finished_at"]
            self._status["summary"] = dict(summary or {})
            self._write_status_locked()
        self.record("INFO", "run", f"Scraper run finished with status {status}.", status=status)

    def set_current_brand(self, brand: Optional[str]) -> None:
        with self._lock:
            self._status["current_brand"] = brand
            self._status["updated_at"] = _now_iso()
            self._write_status_locked()

    def update_totals(self, totals: Dict[str, Any]) -> None:
        with self._lock:
            self._status["totals"] = dict(totals or {})
            self._status["updated_at"] = _now_iso()
            self._write_status_locked()

    def update_brand(self, brand: str, **fields: Any) -> None:
        brand = str(brand or "").strip()
        if not brand:
            return
        with self._lock:
            brands = self._status.setdefault("brands", {})
            brand_entry = brands.setdefault(brand, {
                "brand": brand,
                "status": "PENDING",
                "last_updated_at": _now_iso(),
            })
            for key, value in fields.items():
                if value is not None:
                    brand_entry[key] = value
            brand_entry["last_updated_at"] = _now_iso()
            self._status["updated_at"] = brand_entry["last_updated_at"]
            self._write_status_locked()

    def record(self, level: str, section: str, message: str, **fields: Any) -> None:
        payload = {
            "logged_at": _now_iso(),
            "run_id": self.run_id,
            "level": str(level or "INFO").upper(),
            "section": str(section or "general").lower(),
            "message": str(message or ""),
        }
        for key, value in fields.items():
            if value is not None:
                payload[key] = value

        with self._lock:
            section_counts = self._status.setdefault("section_counts", {})
            section_key = payload["section"]
            section_counts[section_key] = int(section_counts.get(section_key) or 0) + 1
            self._status["updated_at"] = payload["logged_at"]
            self._append_event_locked(payload)
            self._write_status_locked()

    def get_status_copy(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._status)

    def _append_event_locked(self, payload: Dict[str, Any]) -> None:
        try:
            self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.event_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _write_status_locked(self) -> None:
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.status_path.parent / f".status_{secrets.token_hex(6)}.tmp"
            tmp_path.write_text(
                json.dumps(self._status, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.status_path)
        except Exception:
            pass
        finally:
            try:
                if 'tmp_path' in locals() and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


def extract_log_extras(record: logging.LogRecord) -> Dict[str, Any]:
    extras: Dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key not in _STD_LOG_ATTRS and not key.startswith("_"):
            extras[key] = value
    return extras
