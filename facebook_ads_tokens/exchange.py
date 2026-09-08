from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests

from .models import ExchangeResult
from .security import mask_token, redact_query_params

logger = logging.getLogger(__name__)


def exchange_short_token(
    app_id: str,
    app_secret: str,
    short_token: str,
    graph_version: str = "v20.0",
    timeout: int = 60,
) -> ExchangeResult:
    """
    Exchange a short-lived user token for a long-lived user token via Graph OAuth.
    https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived
    """
    url = f"https://graph.facebook.com/{graph_version}/oauth/access_token"
    params: Dict[str, Any] = {
        "grant_type": "fb_exchange_token",
        "client_id": str(app_id).strip(),
        "client_secret": str(app_secret).strip(),
        "fb_exchange_token": str(short_token).strip(),
    }
    logger.info(
        "Token exchange start (short token %s)",
        mask_token(short_token),
    )
    resp = requests.get(url, params=params, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError("Token exchange: invalid JSON response")

    if resp.status_code != 200 or (isinstance(data, dict) and data.get("error")):
        err = data.get("error") if isinstance(data, dict) else data
        logger.error(
            "Token exchange failed status=%s error=%s params=%s",
            resp.status_code,
            err,
            redact_query_params(params),
        )
        raise RuntimeError(f"Token exchange failed: {err}")

    if not isinstance(data, dict):
        raise RuntimeError("Token exchange: unexpected response shape")

    access = str(data.get("access_token", "") or "").strip()
    if not access:
        raise RuntimeError("Token exchange: missing access_token in response")

    expires_in = data.get("expires_in")
    expires_at: Optional[str] = None
    if expires_in is not None:
        try:
            sec = int(expires_in)
            dt = datetime.now(timezone.utc) + timedelta(seconds=sec)
            expires_at = dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            expires_at = None

    token_type = str(data.get("token_type", "bearer") or "bearer")
    logger.info(
        "Token exchange success (new token %s, expires_at=%s)",
        mask_token(access),
        expires_at or "unknown",
    )
    return ExchangeResult(
        access_token=access,
        expires_at=expires_at,
        token_type=token_type,
        raw=dict(data),
    )
