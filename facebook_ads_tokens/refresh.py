from __future__ import annotations

import logging
from typing import Optional

from .exchange import exchange_short_token
from .models import RefreshResult
from .security import mask_token

logger = logging.getLogger(__name__)


def refresh_long_lived_token(
    app_id: str,
    app_secret: str,
    current_long_lived_token: str,
    graph_version: str = "v20.0",
    timeout: int = 60,
) -> RefreshResult:
    """
    Extend / rotate a long-lived user token using the same OAuth exchange endpoint,
    passing the current long-lived token as fb_exchange_token.
    """
    logger.info(
        "Long-lived token refresh start (current %s)",
        mask_token(current_long_lived_token),
    )
    ex = exchange_short_token(
        app_id=app_id,
        app_secret=app_secret,
        short_token=current_long_lived_token,
        graph_version=graph_version,
        timeout=timeout,
    )
    return RefreshResult(
        access_token=ex.access_token,
        expires_at=ex.expires_at,
        token_type=ex.token_type,
        raw=ex.raw,
    )
