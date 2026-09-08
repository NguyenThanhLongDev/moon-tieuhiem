from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass
class TokenRecord:
    """One persisted token row (JSON-serializable)."""

    shop_key: str
    facebook_user_id: str = ""
    ad_account_id: str = ""
    access_token: str = ""
    token_type: str = "bearer"
    issued_at: str = ""
    expires_at: str = ""
    last_refresh_at: str = ""
    refresh_status: str = ""
    note: str = ""

    def to_json_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_dict(cls, data: Dict[str, Any]) -> TokenRecord:
        return cls(
            shop_key=str(data.get("shop_key", "") or ""),
            facebook_user_id=str(data.get("facebook_user_id", "") or ""),
            ad_account_id=str(data.get("ad_account_id", "") or ""),
            access_token=str(data.get("access_token", "") or ""),
            token_type=str(data.get("token_type", "bearer") or "bearer"),
            issued_at=str(data.get("issued_at", "") or ""),
            expires_at=str(data.get("expires_at", "") or ""),
            last_refresh_at=str(data.get("last_refresh_at", "") or ""),
            refresh_status=str(data.get("refresh_status", "") or ""),
            note=str(data.get("note", "") or ""),
        )


@dataclass
class ExchangeResult:
    access_token: str
    expires_at: Optional[str]
    token_type: str
    raw: Dict[str, Any]


@dataclass
class RefreshResult:
    access_token: str
    expires_at: Optional[str]
    token_type: str
    raw: Dict[str, Any]


def default_store_payload(records: List[TokenRecord]) -> Dict[str, Any]:
    return {"version": 1, "records": [r.to_json_dict() for r in records]}


def parse_store_payload(data: Any) -> List[TokenRecord]:
    if not isinstance(data, dict):
        return []
    rows = data.get("records")
    if not isinstance(rows, list):
        return []
    out: List[TokenRecord] = []
    for item in rows:
        if isinstance(item, dict):
            out.append(TokenRecord.from_json_dict(item))
    return out
