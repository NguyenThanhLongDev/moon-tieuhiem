from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from .models import TokenRecord

logger = logging.getLogger(__name__)

TokenHealthStatus = Literal["valid", "expiring_soon", "expired", "refresh_failed"]


def _parse_expires_at(expires_at: str) -> Optional[datetime]:
    text = str(expires_at or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        logger.warning("Unparseable expires_at: %r", expires_at)
        return None


def is_token_expiring_soon(expires_at: str, within_days: int = 7, now: Optional[datetime] = None) -> bool:
    end = _parse_expires_at(expires_at)
    if end is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return end <= ref + timedelta(days=max(0, within_days))


def is_token_expired(expires_at: str, now: Optional[datetime] = None) -> bool:
    end = _parse_expires_at(expires_at)
    if end is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return end <= ref


def should_refresh_record(
    record: TokenRecord,
    expiring_within_days: int = 7,
    now: Optional[datetime] = None,
) -> bool:
    """True if we should call refresh (expired or expiring within window)."""
    if not str(record.access_token or "").strip():
        return False
    end = _parse_expires_at(record.expires_at)
    if end is None:
        return False
    ref = now or datetime.now(timezone.utc)
    if end <= ref:
        return True
    return end <= ref + timedelta(days=max(0, expiring_within_days))


def get_token_health_status(
    record: TokenRecord,
    expiring_within_days: int = 7,
    now: Optional[datetime] = None,
) -> TokenHealthStatus:
    rs = str(record.refresh_status or "").strip().lower()
    if rs in {"refresh_failed", "failed"}:
        return "refresh_failed"
    if not str(record.access_token or "").strip():
        return "expired"
    end = _parse_expires_at(record.expires_at)
    if end is None:
        return "valid"
    ref = now or datetime.now(timezone.utc)
    if end <= ref:
        return "expired"
    if end <= ref + timedelta(days=max(0, expiring_within_days)):
        return "expiring_soon"
    return "valid"
