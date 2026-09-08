from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn
from repositories.admin_repo import get_fb_ad_account_mappings
from facebook_ads_tokens.resolve import try_store_access_token
from fb_currency import convert_to_vnd


GRAPH_API_VERSION = os.getenv("FACEBOOK_GRAPH_API_VERSION", "v20.0")

_global_access_token: Optional[str] = None
_global_access_token_error: Optional[BaseException] = None


def access_token_for_mapping(shop_key: str, fb_ad_account_id: str) -> str:
    """Per-row: store token if mapped; else lazy global (env/config) same as before."""
    global _global_access_token, _global_access_token_error
    stored = try_store_access_token(shop_key, fb_ad_account_id)
    if stored:
        return stored
    if _global_access_token_error is not None:
        raise _global_access_token_error
    if _global_access_token is not None:
        return _global_access_token
    try:
        _global_access_token = require_access_token()
        return _global_access_token
    except Exception as exc:
        _global_access_token_error = exc
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Facebook Ads spend into DB.")
    parser.add_argument(
        "--date",
        default="",
        help="Single target day YYYY-MM-DD (use this OR --date-from + --date-to).",
    )
    parser.add_argument(
        "--date-from",
        dest="date_from",
        default="",
        help="First day inclusive YYYY-MM-DD (backfill range with --date-to).",
    )
    parser.add_argument(
        "--date-to",
        dest="date_to",
        default="",
        help="Last day inclusive YYYY-MM-DD.",
    )
    parser.add_argument("--shop-key", default="", help="Optional shop_key to sync one mapping only.")
    parser.add_argument("--fb-ad-account-id", default="", help="Optional Facebook Ad Account ID to sync one account only.")
    return parser.parse_args()


def resolve_target_dates(args: argparse.Namespace) -> List[str]:
    """Return sorted list of YYYY-MM-DD to sync (one day or inclusive range)."""
    one = str(getattr(args, "date", "") or "").strip()
    df = str(getattr(args, "date_from", "") or "").strip()
    dt = str(getattr(args, "date_to", "") or "").strip()
    if one:
        if df or dt:
            raise SystemExit("Use either --date or (--date-from and --date-to), not both.")
        datetime.strptime(one, "%Y-%m-%d")
        return [one]
    if df and dt:
        datetime.strptime(df, "%Y-%m-%d")
        datetime.strptime(dt, "%Y-%m-%d")
        a = datetime.strptime(df, "%Y-%m-%d").date()
        b = datetime.strptime(dt, "%Y-%m-%d").date()
        if a > b:
            a, b = b, a
        out: List[str] = []
        cur = a
        while cur <= b:
            out.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return out
    raise SystemExit("Required: --date YYYY-MM-DD or both --date-from and --date-to (inclusive).")


FB_ONSITE_MSG_CONV_STARTED_7D = "onsite_conversion.messaging_conversation_started_7d"

# Purchase actions: first match in API wins (safest attribution).
PURCHASE_ACTION_PRIORITY: tuple[str, ...] = (
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
    "purchase",
)


def _is_messaging_conversation_action(action_type: str) -> bool:
    return str(action_type or "").strip() == FB_ONSITE_MSG_CONV_STARTED_7D


def extract_message_count_from_insights_row(spend: float, row: Dict[str, Any]) -> Optional[int]:
    """cost_per_action_type → spend/cost; else actions count; else no row → None ('-')."""
    actions = row.get("actions")
    cpa = row.get("cost_per_action_type")

    cpa_cost: Optional[float] = None
    if isinstance(cpa, list):
        for item in cpa:
            if not isinstance(item, dict):
                continue
            if not _is_messaging_conversation_action(str(item.get("action_type", ""))):
                continue
            try:
                cpa_cost = float(str(item.get("value", 0) or 0))
            except (TypeError, ValueError):
                cpa_cost = None
            break

    if cpa_cost is not None and cpa_cost > 0 and spend > 0:
        return max(1, int(round(spend / cpa_cost)))

    count_from_actions = 0
    actions_is_list = isinstance(actions, list)
    if actions_is_list:
        for item in actions:
            if not isinstance(item, dict):
                continue
            if not _is_messaging_conversation_action(str(item.get("action_type", ""))):
                continue
            try:
                count_from_actions += int(float(str(item.get("value", 0) or 0)))
            except (TypeError, ValueError):
                pass

    if count_from_actions > 0:
        return count_from_actions

    if actions_is_list:
        return 0

    if isinstance(cpa, list):
        return 0

    return None


def extract_purchase_metrics_from_insights_row(row: Dict[str, Any]) -> tuple[Optional[int], Optional[float], Optional[str]]:
    """Returns (purchase_count, cost_per_purchase from API or None, action_type used for count)."""
    actions = row.get("actions")
    cpa = row.get("cost_per_action_type")
    used_type: Optional[str] = None
    count: Optional[int] = None

    if isinstance(actions, list):
        for ptype in PURCHASE_ACTION_PRIORITY:
            for item in actions:
                if not isinstance(item, dict):
                    continue
                if str(item.get("action_type", "")).strip() != ptype:
                    continue
                try:
                    count = int(float(str(item.get("value", 0) or 0)))
                except (TypeError, ValueError):
                    count = 0
                used_type = ptype
                break
            if used_type is not None:
                break
        if count is None and actions is not None:
            count = 0

    cpa_val: Optional[float] = None
    if isinstance(cpa, list):
        for ptype in PURCHASE_ACTION_PRIORITY:
            for item in cpa:
                if not isinstance(item, dict):
                    continue
                if str(item.get("action_type", "")).strip() != ptype:
                    continue
                try:
                    raw = float(str(item.get("value", 0) or 0))
                except (TypeError, ValueError):
                    raw = 0.0
                if raw > 0:
                    cpa_val = raw
                break
            if cpa_val is not None:
                break

    return count, cpa_val, used_type


def normalize_account_id(raw_value: str) -> str:
    text = str(raw_value or "").strip()
    if text.startswith("act_"):
        text = text[4:]
    return text


def require_access_token() -> str:
    token = str(os.getenv("FACEBOOK_ACCESS_TOKEN", "")).strip()
    if not token:
        config_path = BASE_DIR / "config.json"
        if config_path.exists():
            try:
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                token = str((payload or {}).get("facebook_access_token", "")).strip()
            except Exception:
                token = ""
    if not token:
        raise RuntimeError("FACEBOOK_ACCESS_TOKEN is required.")
    return token


def fetch_account_daily_metrics(access_token: str, fb_ad_account_id: str, target_date: str) -> Dict[str, Any]:
    normalized_account_id = normalize_account_id(fb_ad_account_id)
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{normalized_account_id}/insights"
    params = {
        "access_token": access_token,
        "fields": (
            "spend,impressions,clicks,account_id,account_name,account_currency,date_start,date_stop,"
            "actions,cost_per_action_type"
        ),
        "level": "account",
        "time_increment": 1,
        "time_range": json.dumps({"since": target_date, "until": target_date}),
        "limit": 1000,
    }
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"Facebook insights error: {payload.get('error')}")
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        return {
            "spend": 0.0,
            "impressions": 0,
            "clicks": 0,
            "account_name": "",
            "message_count": None,
            "purchase_count": None,
            "purchase_cpa": None,
            "purchase_action_type": None,
            "_api_empty_payload": True,
        }
    row = rows[0] if isinstance(rows[0], dict) else {}
    spend_raw = float(row.get("spend", 0) or 0)
    currency = (row.get("account_currency") or "VND").upper()
    # Convert sang VND nếu account_currency != VND (vd USD → × tỷ giá ACB bán ra)
    spend = convert_to_vnd(spend_raw, currency)
    pc, pcpa, pat = extract_purchase_metrics_from_insights_row(row)
    return {
        "spend": spend,
        "_currency": currency,
        "_spend_raw": spend_raw,
        "impressions": int(float(row.get("impressions", 0) or 0)),
        "clicks": int(float(row.get("clicks", 0) or 0)),
        "account_name": str(row.get("account_name", "") or "").strip(),
        "message_count": extract_message_count_from_insights_row(spend, row),
        "purchase_count": pc,
        "purchase_cpa": pcpa,
        "purchase_action_type": pat,
        "_api_empty_payload": False,
    }


def load_target_mappings(shop_key: str, fb_ad_account_id: str) -> List[Dict[str, Any]]:
    """Load mappings để sync. Bao gồm CẢ inactive — quy tắc bất biến của
    sếp (2026-05-18): 'có chi tiêu là phải hiển thị'. TK inactive vẫn cần
    pull spend; kế toán xem riêng nếu cần xử lý.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            return get_fb_ad_account_mappings(
                cur,
                shop_key=shop_key or None,
                fb_ad_account_id=normalize_account_id(fb_ad_account_id) or None,
                active_only=False,
            )


def upsert_fb_daily_metric(
    shop_id: int,
    metric_date: str,
    fb_ad_account_id: str,
    account_name: str,
    spend: float,
    impressions: int,
    clicks: int,
    message_count: Optional[int] = None,
    purchase_count: Optional[int] = None,
    purchase_cpa: Optional[float] = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO fb_ads_daily_metrics (
                        shop_id, metric_date, fb_ad_account_id, account_name, spend, impressions, clicks,
                        message_count, purchase_count, purchase_cpa
                    )
                    VALUES (%s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (shop_id, metric_date, fb_ad_account_id)
                    DO UPDATE SET
                        account_name = EXCLUDED.account_name,
                        spend = EXCLUDED.spend,
                        impressions = EXCLUDED.impressions,
                        clicks = EXCLUDED.clicks,
                        message_count = EXCLUDED.message_count,
                        purchase_count = EXCLUDED.purchase_count,
                        purchase_cpa = EXCLUDED.purchase_cpa
                    """,
                    (
                        shop_id,
                        metric_date,
                        normalize_account_id(fb_ad_account_id),
                        account_name,
                        spend,
                        impressions,
                        clicks,
                        message_count,
                        purchase_count,
                        purchase_cpa,
                    ),
                )
            except Exception as exc:
                conn.rollback()
                text = str(exc)
                if ("purchase_count" in text or "purchase_cpa" in text) and "does not exist" in text:
                    try:
                        cur.execute(
                            """
                            INSERT INTO fb_ads_daily_metrics (
                                shop_id, metric_date, fb_ad_account_id, account_name, spend, impressions, clicks, message_count
                            )
                            VALUES (%s, %s::date, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (shop_id, metric_date, fb_ad_account_id)
                            DO UPDATE SET
                                account_name = EXCLUDED.account_name,
                                spend = EXCLUDED.spend,
                                impressions = EXCLUDED.impressions,
                                clicks = EXCLUDED.clicks,
                                message_count = EXCLUDED.message_count
                            """,
                            (
                                shop_id,
                                metric_date,
                                normalize_account_id(fb_ad_account_id),
                                account_name,
                                spend,
                                impressions,
                                clicks,
                                message_count,
                            ),
                        )
                    except Exception as exc2:
                        conn.rollback()
                        text2 = str(exc2)
                        if "message_count" not in text2 or "does not exist" not in text2:
                            raise exc2 from exc
                        cur.execute(
                            """
                            INSERT INTO fb_ads_daily_metrics (
                                shop_id, metric_date, fb_ad_account_id, account_name, spend, impressions, clicks
                            )
                            VALUES (%s, %s::date, %s, %s, %s, %s, %s)
                            ON CONFLICT (shop_id, metric_date, fb_ad_account_id)
                            DO UPDATE SET
                                account_name = EXCLUDED.account_name,
                                spend = EXCLUDED.spend,
                                impressions = EXCLUDED.impressions,
                                clicks = EXCLUDED.clicks
                            """,
                            (
                                shop_id,
                                metric_date,
                                normalize_account_id(fb_ad_account_id),
                                account_name,
                                spend,
                                impressions,
                                clicks,
                            ),
                        )
                    return
                if "message_count" in text and "does not exist" in text:
                    try:
                        cur.execute(
                            """
                            INSERT INTO fb_ads_daily_metrics (
                                shop_id, metric_date, fb_ad_account_id, account_name, spend, impressions, clicks
                            )
                            VALUES (%s, %s::date, %s, %s, %s, %s, %s)
                            ON CONFLICT (shop_id, metric_date, fb_ad_account_id)
                            DO UPDATE SET
                                account_name = EXCLUDED.account_name,
                                spend = EXCLUDED.spend,
                                impressions = EXCLUDED.impressions,
                                clicks = EXCLUDED.clicks
                            """,
                            (
                                shop_id,
                                metric_date,
                                normalize_account_id(fb_ad_account_id),
                                account_name,
                                spend,
                                impressions,
                                clicks,
                            ),
                        )
                    except Exception as exc2:
                        conn.rollback()
                        text2 = str(exc2)
                        if "account_name" not in text2 or "does not exist" not in text2:
                            raise exc2 from exc
                        cur.execute(
                            """
                            INSERT INTO fb_ads_daily_metrics (
                                shop_id, metric_date, fb_ad_account_id, spend, impressions, clicks
                            )
                            VALUES (%s, %s::date, %s, %s, %s, %s)
                            ON CONFLICT (shop_id, metric_date, fb_ad_account_id)
                            DO UPDATE SET
                                spend = EXCLUDED.spend,
                                impressions = EXCLUDED.impressions,
                                clicks = EXCLUDED.clicks
                            """,
                            (
                                shop_id,
                                metric_date,
                                normalize_account_id(fb_ad_account_id),
                                spend,
                                impressions,
                                clicks,
                            ),
                        )
                    return
                if "account_name" not in text or "does not exist" not in text:
                    raise exc
                # Old schema: no account_name (no message_count).
                conn.rollback()
                cur.execute(
                    """
                    INSERT INTO fb_ads_daily_metrics (
                        shop_id, metric_date, fb_ad_account_id, spend, impressions, clicks
                    )
                    VALUES (%s, %s::date, %s, %s, %s, %s)
                    ON CONFLICT (shop_id, metric_date, fb_ad_account_id)
                    DO UPDATE SET
                        spend = EXCLUDED.spend,
                        impressions = EXCLUDED.impressions,
                        clicks = EXCLUDED.clicks
                    """,
                    (
                        shop_id,
                        metric_date,
                        normalize_account_id(fb_ad_account_id),
                        spend,
                        impressions,
                        clicks,
                    ),
                )


def main() -> None:
    args = parse_args()
    target_dates = resolve_target_dates(args)
    mappings = load_target_mappings(args.shop_key.strip(), args.fb_ad_account_id.strip())
    if not mappings:
        print("No active Facebook Ads mappings found for the given filters.")
        return

    synced = 0
    failed = 0
    zero_spend_warnings = 0
    touched_shops = set()
    nd = len(target_dates)
    print(f"SYNC_DATE_RANGE days={nd} first={target_dates[0]} last={target_dates[-1]} mappings={len(mappings)}")

    for target_date in target_dates:
        for mapping in mappings:
            shop_key = str(mapping.get("shop_key", "")).strip()
            shop_id = int(mapping.get("shop_id"))
            fb_ad_account_id = str(mapping.get("fb_ad_account_id", "")).strip()
            mapped_account_name = str(mapping.get("account_name", "")).strip()
            aid = normalize_account_id(fb_ad_account_id)
            print(f"SYNC_BEGIN shop={shop_key} shop_id={shop_id} account_id={aid} date={target_date}")
            try:
                access_token = access_token_for_mapping(shop_key, fb_ad_account_id)
                metrics = fetch_account_daily_metrics(access_token, fb_ad_account_id, target_date)
                empty_pl = bool(metrics.pop("_api_empty_payload", False))
                print(
                    f"SYNC_API_OK shop={shop_key} account_id={aid} date={target_date} "
                    f"api_empty_payload={empty_pl} spend={metrics.get('spend')} impressions={metrics.get('impressions')} "
                    f"clicks={metrics.get('clicks')}"
                )
                account_name = str(metrics.get("account_name", "") or "").strip() or mapped_account_name
                upsert_fb_daily_metric(
                    shop_id=shop_id,
                    metric_date=target_date,
                    fb_ad_account_id=fb_ad_account_id,
                    account_name=account_name,
                    spend=float(metrics["spend"]),
                    impressions=int(metrics["impressions"]),
                    clicks=int(metrics["clicks"]),
                    message_count=metrics.get("message_count"),
                    purchase_count=metrics.get("purchase_count"),
                    purchase_cpa=metrics.get("purchase_cpa"),
                )
                synced += 1
                touched_shops.add(shop_key)
                print(f"SYNC_DB_OK shop={shop_key} account_id={aid} date={target_date} rows_written=1")
                if float(metrics["spend"]) == 0:
                    zero_spend_warnings += 1
                    print(
                        f"WARN_ZERO_SPEND shop={shop_key} account_id={aid} date={target_date}"
                    )
                print(
                    f"SYNCED shop={shop_key} account_id={aid} date={target_date} spend={metrics['spend']} "
                    f"impressions={metrics['impressions']} clicks={metrics['clicks']} "
                    f"messages={metrics.get('message_count')} purchases={metrics.get('purchase_count')} "
                    f"purchase_type={metrics.get('purchase_action_type')}"
                )
            except Exception as exc:
                failed += 1
                print(
                    f"SYNC_FAIL shop={shop_key} account_id={aid} date={target_date} api_or_db=error error={exc!r}"
                )

    print(
        f"SUMMARY days={nd} accounts_synced={synced} failed={failed} "
        f"total_mapping_accounts={len(mappings)} shops_affected={len(touched_shops)} "
        f"zero_spend_warnings={zero_spend_warnings}"
    )


if __name__ == "__main__":
    main()
