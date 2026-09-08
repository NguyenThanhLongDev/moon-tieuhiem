#!/usr/bin/env python3
"""Kéo "Kết quả" (Result) campaign từ Facebook về fb_campaign_results_daily.

"Kết quả" = chỉ số chính của campaign tuỳ mục tiêu (tin nhắn / lead / mua hàng...).
Lưu THEO NGÀY (time_increment=1) để báo cáo cộng dồn đúng khoảng ngày → khớp cột
"Kết quả" + "Chi phí trên mỗi kết quả" của FB Ads Manager.

Token: env FACEBOOK_ACCESS_TOKEN. Account: pa_ad_accounts.
Chạy:  export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/sync_fb_campaign_results.py --days 14
"""
import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("sync_fb_campaign_results")

GRAPH = "https://graph.facebook.com/v20.0"

# Ưu tiên chọn "kết quả chính" theo mục tiêu campaign (giống FB Ads Manager)
RESULT_PRIORITY = [
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.total_messaging_connection",
    "offsite_complete_registration_add_meta_leads",
    "onsite_conversion.lead_grouped",
    "lead",
    "leadgen.other",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
    "purchase",
    "offsite_conversion.fb_pixel_add_to_cart",
    "link_click",
]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {}
    except Exception as e:
        return {"_err": str(e)}


def primary_result(actions):
    am = {}
    for a in (actions or []):
        try:
            am[a.get("action_type", "")] = int(float(a.get("value", 0)))
        except Exception:
            pass
    for k in RESULT_PRIORITY:
        if am.get(k):
            return am[k], k
    return 0, ""


def fetch_results(token, account_id, since, until):
    """[(metric_date, campaign_id, results, result_type)] theo từng ngày."""
    out = []
    tr = json.dumps({"since": since, "until": until})
    url = (f"{GRAPH}/act_{account_id}/insights?level=campaign&time_increment=1"
           f"&fields=campaign_id,actions&time_range={urllib.parse.quote(tr)}"
           f"&limit=300&access_token={urllib.parse.quote(token)}")
    pages = 0
    while url and pages < 50:
        js = _get(url)
        if "data" not in js:
            err = js.get("error", {}).get("message", "")
            if err:
                logger.info("  [%s] %s", account_id, err[:70])
            break
        for d in js["data"]:
            res, typ = primary_result(d.get("actions"))
            out.append((d.get("date_start", ""), str(d.get("campaign_id", "")), res, typ))
        url = js.get("paging", {}).get("next")
        pages += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--date-from", dest="date_from", default="")
    ap.add_argument("--date-to", dest="date_to", default="")
    args = ap.parse_args()

    token = (os.getenv("FACEBOOK_ACCESS_TOKEN", "") or "").strip()
    if not token:
        logger.error("Thiếu FACEBOOK_ACCESS_TOKEN.")
        sys.exit(1)

    if args.date_from and args.date_to:
        since, until = args.date_from, args.date_to
    else:
        until = date.today().isoformat()
        since = (date.today() - timedelta(days=args.days)).isoformat()

    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT REPLACE(account_id,'act_','') FROM pa_ad_accounts")
            accounts = [str(r[0]) for r in cur.fetchall()]

    logger.info("=== sync campaign results %s..%s: %d TK ===", since, until, len(accounts))
    total = 0
    ok = 0
    with get_conn() as conn:
        for aid in accounts:
            rows = fetch_results(token, aid, since, until)
            if not rows:
                continue
            ok += 1
            with conn.cursor() as cur:
                for mdate, cid, res, typ in rows:
                    if not cid or not mdate:
                        continue
                    cur.execute(
                        """INSERT INTO fb_campaign_results_daily (metric_date, campaign_id, account_id, results, result_type, updated_at)
                           VALUES (%s,%s,%s,%s,%s, now())
                           ON CONFLICT (metric_date, campaign_id) DO UPDATE SET
                             account_id=EXCLUDED.account_id, results=EXCLUDED.results,
                             result_type=EXCLUDED.result_type, updated_at=now()""",
                        (mdate, cid, aid, res, typ),
                    )
                    total += 1
            conn.commit()
    logger.info("=== DONE: %d dòng kết quả từ %d/%d TK ===", total, ok, len(accounts))


if __name__ == "__main__":
    main()
