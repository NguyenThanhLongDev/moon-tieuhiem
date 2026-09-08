from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from .models import TokenRecord, default_store_payload, parse_store_payload

logger = logging.getLogger(__name__)


def load_store(path: Path) -> List[TokenRecord]:
    path = Path(path)
    if not path.exists():
        logger.info("Token store missing; starting empty: %s", path)
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read token store %s: %s", path, exc)
        raise
    return parse_store_payload(raw)


def save_store(path: Path, records: List[TokenRecord]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = default_store_payload(records)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logger.info("Saved token store: %s (%d records)", path, len(records))


def find_record_index(
    records: List[TokenRecord], shop_key: str, ad_account_id: str
) -> Optional[int]:
    sk = str(shop_key or "").strip()
    aid = str(ad_account_id or "").strip()
    for i, r in enumerate(records):
        if r.shop_key == sk and r.ad_account_id == aid:
            return i
    return None


def upsert_record(records: List[TokenRecord], record: TokenRecord) -> Tuple[List[TokenRecord], bool]:
    """Return (new_list, inserted)."""
    idx = find_record_index(records, record.shop_key, record.ad_account_id)
    out = list(records)
    if idx is None:
        out.append(record)
        return out, True
    out[idx] = record
    return out, False
