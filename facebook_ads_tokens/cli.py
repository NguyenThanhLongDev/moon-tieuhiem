from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .client import FacebookAdsClient
from .exchange import exchange_short_token
from .health import get_token_health_status, should_refresh_record
from .models import TokenRecord
from .refresh import refresh_long_lived_token
from .security import mask_token
from .storage import find_record_index, load_store, save_store, upsert_record


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_store_path() -> Path:
    raw = str(os.getenv("FB_ADS_TOKENS_STORE", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "facebook_ads_tokens_store.json"


def _graph_version() -> str:
    return str(os.getenv("FACEBOOK_GRAPH_API_VERSION", "v20.0") or "v20.0").strip()


def _app_creds() -> Tuple[str, str]:
    app_id = str(os.getenv("FACEBOOK_APP_ID", "") or "").strip()
    secret = str(os.getenv("FACEBOOK_APP_SECRET", "") or "").strip()
    return app_id, secret


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cmd_exchange(args: argparse.Namespace) -> int:
    app_id, secret = _app_creds()
    if not app_id or not secret:
        logging.error("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are required.")
        return 2
    store = Path(args.store)
    try:
        ex = exchange_short_token(
            app_id, secret, args.short_token, graph_version=_graph_version()
        )
    except Exception as exc:
        logging.error("Exchange failed: %s", exc)
        return 1
    records = load_store(store)
    rec = TokenRecord(
        shop_key=str(args.shop_key).strip(),
        facebook_user_id=str(args.facebook_user_id or "").strip(),
        ad_account_id=str(args.ad_account_id or "").strip(),
        access_token=ex.access_token,
        token_type=ex.token_type,
        issued_at=_utc_now_iso(),
        expires_at=ex.expires_at or "",
        last_refresh_at="",
        refresh_status="ok",
        note=str(args.note or "").strip(),
    )
    records, _ = upsert_record(records, rec)
    save_store(store, records)
    logging.info("Stored token for shop_key=%s ad_account_id=%s", rec.shop_key, rec.ad_account_id)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    app_id, secret = _app_creds()
    if not app_id or not secret:
        logging.error("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are required.")
        return 2
    store = Path(args.store)
    records = load_store(store)
    idx = find_record_index(records, args.shop_key, args.ad_account_id or "")
    if idx is None:
        logging.error("No record for shop_key=%s ad_account_id=%s", args.shop_key, args.ad_account_id)
        return 1
    rec = records[idx]
    try:
        ref = refresh_long_lived_token(
            app_id, secret, rec.access_token, graph_version=_graph_version()
        )
    except Exception as exc:
        logging.error("Refresh failed: %s", exc)
        rec.last_refresh_at = _utc_now_iso()
        rec.refresh_status = "refresh_failed"
        records[idx] = rec
        save_store(store, records)
        return 1
    rec.access_token = ref.access_token
    rec.token_type = ref.token_type
    rec.expires_at = ref.expires_at or rec.expires_at
    rec.last_refresh_at = _utc_now_iso()
    rec.refresh_status = "ok"
    records[idx] = rec
    save_store(store, records)
    logging.info(
        "Refresh OK shop_key=%s token=%s",
        rec.shop_key,
        mask_token(ref.access_token),
    )
    return 0


def cmd_refresh_all(args: argparse.Namespace) -> int:
    app_id, secret = _app_creds()
    if not app_id or not secret:
        logging.error("FACEBOOK_APP_ID and FACEBOOK_APP_SECRET are required.")
        return 2
    store = Path(args.store)
    records = load_store(store)
    days = int(args.within_days)
    any_fail = False
    for i, rec in enumerate(list(records)):
        if not should_refresh_record(rec, expiring_within_days=days):
            status = get_token_health_status(rec, expiring_within_days=days)
            logging.info(
                "Skip shop_key=%s ad_account=%s health=%s",
                rec.shop_key,
                rec.ad_account_id,
                status,
            )
            continue
        logging.info(
            "Refresh start shop_key=%s ad_account=%s (expiring window=%dd)",
            rec.shop_key,
            rec.ad_account_id,
            days,
        )
        try:
            ref = refresh_long_lived_token(
                app_id, secret, rec.access_token, graph_version=_graph_version()
            )
            rec.access_token = ref.access_token
            rec.token_type = ref.token_type
            rec.expires_at = ref.expires_at or rec.expires_at
            rec.last_refresh_at = _utc_now_iso()
            rec.refresh_status = "ok"
            records[i] = rec
            logging.info("Refresh success shop_key=%s", rec.shop_key)
        except Exception as exc:
            logging.error("Refresh failure shop_key=%s: %s", rec.shop_key, exc)
            rec.last_refresh_at = _utc_now_iso()
            rec.refresh_status = "refresh_failed"
            records[i] = rec
            any_fail = True
    save_store(store, records)
    return 1 if any_fail else 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Path(args.store)
    records = load_store(store)
    days = int(args.within_days)
    if not records:
        logging.info("No records in store.")
        return 0
    for rec in records:
        st = get_token_health_status(rec, expiring_within_days=days)
        logging.info(
            "shop_key=%s ad_account_id=%s health=%s expires_at=%s refresh_status=%s token=%s",
            rec.shop_key,
            rec.ad_account_id,
            st,
            rec.expires_at or "-",
            rec.refresh_status or "-",
            mask_token(rec.access_token),
        )
    return 0


def _token_for_cli(args: argparse.Namespace) -> Optional[str]:
    if str(args.access_token or "").strip():
        return str(args.access_token).strip()
    store = Path(args.store)
    records = load_store(store)
    idx = find_record_index(records, args.shop_key, args.ad_account_id or "")
    if idx is None:
        return None
    return records[idx].access_token or None


def cmd_test_adaccounts(args: argparse.Namespace) -> int:
    token = _token_for_cli(args)
    if not token:
        logging.error("No access token: pass --access-token or a matching store record.")
        return 1
    client = FacebookAdsClient(token, graph_version=_graph_version())
    try:
        data = client.get_me_adaccounts()
    except Exception as exc:
        logging.error("adaccounts failed: %s", exc)
        return 1
    logging.info("adaccounts OK keys=%s", list(data.keys()))
    for item in data.get("data", [])[:20]:
        logging.info("  %s", item)
    return 0


def cmd_test_insights(args: argparse.Namespace) -> int:
    token = _token_for_cli(args)
    if not token:
        logging.error("No access token: pass --access-token or a matching store record.")
        return 1
    client = FacebookAdsClient(token, graph_version=_graph_version())
    try:
        data = client.get_account_insights(args.ad_account_id, args.date, args.date)
    except Exception as exc:
        logging.error("insights failed: %s", exc)
        return 1
    logging.info("insights OK keys=%s", list(data.keys()))
    for row in data.get("data", [])[:10]:
        logging.info("  %s", row)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Facebook Ads token store / exchange / refresh (isolated module).",
    )
    p.add_argument(
        "--store",
        default=str(default_store_path()),
        help="Path to JSON token store (default: FB_ADS_TOKENS_STORE or ./facebook_ads_tokens_store.json)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("exchange", help="Short-lived → long-lived, then upsert store row")
    ex.add_argument("--shop-key", required=True)
    ex.add_argument("--short-token", required=True)
    ex.add_argument("--facebook-user-id", default="")
    ex.add_argument("--ad-account-id", default="")
    ex.add_argument("--note", default="")
    ex.set_defaults(func=cmd_exchange)

    rf = sub.add_parser("refresh", help="Refresh one store row")
    rf.add_argument("--shop-key", required=True)
    rf.add_argument("--ad-account-id", default="")
    rf.set_defaults(func=cmd_refresh)

    ra = sub.add_parser("refresh-all", help="Refresh rows that expire within --within-days or are expired")
    ra.add_argument("--within-days", type=int, default=7)
    ra.set_defaults(func=cmd_refresh_all)

    st = sub.add_parser("status", help="Print health for all rows")
    st.add_argument("--within-days", type=int, default=7)
    st.set_defaults(func=cmd_status)

    ta = sub.add_parser("test-adaccounts", help="GET /me/adaccounts")
    ta.add_argument("--shop-key", default="")
    ta.add_argument("--ad-account-id", default="")
    ta.add_argument("--access-token", default="")
    ta.set_defaults(func=cmd_test_adaccounts)

    ti = sub.add_parser("test-insights", help="GET /act_{id}/insights for one day")
    ti.add_argument("--ad-account-id", required=True)
    ti.add_argument("--date", required=True, help="YYYY-MM-DD")
    ti.add_argument("--shop-key", default="")
    ti.add_argument("--access-token", default="")
    ti.set_defaults(func=cmd_test_insights)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
