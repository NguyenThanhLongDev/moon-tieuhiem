from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import requests

from .security import mask_token, redact_query_params

logger = logging.getLogger(__name__)


def normalize_ad_account_id(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("act_"):
        text = text[4:]
    return text


class FacebookAdsClient:
    """Minimal Graph client: ad accounts list + account insights (no token in logs)."""

    def __init__(
        self,
        access_token: str,
        graph_version: str = "v20.0",
        timeout: int = 60,
        max_retries: int = 2,
    ) -> None:
        self._token = str(access_token or "").strip()
        self._graph_version = str(graph_version or "v20.0").strip()
        self._timeout = int(timeout)
        self._max_retries = max(0, int(max_retries))

    def _request(self, method: str, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = {**params, "access_token": self._token}
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                logger.debug(
                    "Graph %s %s params=%s",
                    method,
                    url,
                    redact_query_params(params),
                )
                resp = requests.request(method, url, params=params, timeout=self._timeout)
                data = resp.json()
                if resp.status_code >= 500 and attempt < self._max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(f"Graph API error: {data.get('error')}")
                resp.raise_for_status()
                if not isinstance(data, dict):
                    raise RuntimeError("Graph API: expected JSON object")
                return data
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_exc = exc
                if attempt < self._max_retries and _is_retriable(exc):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(str(last_exc))

    def get_me_adaccounts(self, fields: str = "id,name,account_id") -> Dict[str, Any]:
        url = f"https://graph.facebook.com/{self._graph_version}/me/adaccounts"
        logger.info("Fetching /me/adaccounts (token %s)", mask_token(self._token))
        return self._request("GET", url, {"fields": fields})

    def get_ads_with_creatives(
        self,
        ad_account_id: str,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Fetch all ads with creative object_story_id (format: page_id_post_id)."""
        aid = normalize_ad_account_id(ad_account_id)
        url = f"https://graph.facebook.com/{self._graph_version}/act_{aid}/ads"
        params: Dict[str, Any] = {
            "fields": "id,name,status,creative{object_story_id}",
            "limit": limit,
        }
        logger.info("Fetching ads+creatives act_%s (token %s)", aid, mask_token(self._token))
        return self._request("GET", url, params)

    def get_ad_level_insights(
        self,
        ad_account_id: str,
        date_start: str,
        date_stop: str,
        fields: str = "ad_id,ad_name,spend,impressions,clicks,date_start",
    ) -> Dict[str, Any]:
        """Get insights at ad level (1-day increment) — joins with creative map for page spend."""
        aid = normalize_ad_account_id(ad_account_id)
        url = f"https://graph.facebook.com/{self._graph_version}/act_{aid}/insights"
        params: Dict[str, Any] = {
            "fields": fields,
            "level": "ad",
            "time_increment": 1,
            "time_range": json.dumps({"since": date_start, "until": date_stop}),
            "limit": 500,
        }
        logger.info(
            "Fetching ad-level insights act_%s %s..%s (token %s)",
            aid, date_start, date_stop, mask_token(self._token),
        )
        return self._request("GET", url, params)

    def get_account_insights(
        self,
        ad_account_id: str,
        date_start: str,
        date_stop: str,
        fields: str = "spend,impressions,clicks,account_id,account_name,date_start,date_stop",
    ) -> Dict[str, Any]:
        aid = normalize_ad_account_id(ad_account_id)
        url = f"https://graph.facebook.com/{self._graph_version}/act_{aid}/insights"
        params: Dict[str, Any] = {
            "fields": fields,
            "level": "account",
            "time_increment": 1,
            "time_range": json.dumps({"since": date_start, "until": date_stop}),
            "limit": 100,
        }
        logger.info(
            "Fetching insights act_%s %s..%s (token %s)",
            aid,
            date_start,
            date_stop,
            mask_token(self._token),
        )
        return self._request("GET", url, params)


def _is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, requests.Timeout):
        return True
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500
    return False
