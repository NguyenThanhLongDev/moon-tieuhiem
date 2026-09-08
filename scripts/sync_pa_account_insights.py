"""
Sync spend từ FB Graph API cho tất cả pa_ad_accounts
dùng token Trần Huy (pa_fb_tokens).
Dùng batch API — 50 TK / request.
"""
import os, sys, json, logging, requests
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_API = "https://graph.facebook.com/v19.0"
BATCH_SIZE = 50


def get_token(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT access_token FROM pa_fb_tokens ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None


def get_all_account_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT account_id FROM pa_ad_accounts ORDER BY account_id")
        return [r[0] for r in cur.fetchall()]


def batch_insights(token: str, account_ids: list[str], days: int = 3) -> dict:
    """Trả về {raw_id: {date: spend}}"""
    date_from = (date.today() - timedelta(days=days)).isoformat()
    date_to   = date.today().isoformat()
    result = {}

    for i in range(0, len(account_ids), BATCH_SIZE):
        chunk = account_ids[i:i + BATCH_SIZE]
        batch = []
        for act_id in chunk:
            params = urlencode({
                "fields": "spend",
                "time_increment": "1",
                "time_range": json.dumps({"since": str(date_from), "until": str(date_to)}),
            })
            batch.append({
                "method": "GET",
                "relative_url": f"{act_id}/insights?{params}",
            })

        resp = requests.post(
            FB_API,
            data={"access_token": token, "batch": json.dumps(batch)},
            timeout=60,
        )
        resp.raise_for_status()
        responses = resp.json()

        for j, item in enumerate(responses):
            act_id = chunk[j]
            raw_id = act_id.replace("act_", "")
            if item is None or item.get("code") != 200:
                continue
            try:
                body = json.loads(item["body"])
                for row in body.get("data", []):
                    d = row.get("date_start")
                    s = int(float(row.get("spend", 0)))
                    if d and s > 0:
                        result.setdefault(raw_id, {})[d] = s
            except Exception:
                pass

        logger.info(f"  Batch {i//BATCH_SIZE + 1}/{(len(account_ids)-1)//BATCH_SIZE + 1} done")

    return result


def upsert_insights(conn, data: dict):
    rows = []
    for raw_id, date_map in data.items():
        for d, spend in date_map.items():
            rows.append((raw_id, d, spend))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO pa_account_insights (account_id, metric_date, spend, synced_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (account_id, metric_date) DO UPDATE
            SET spend=EXCLUDED.spend, synced_at=NOW()
        """, rows)
    conn.commit()
    return len(rows)


def main(days: int = 3):
    from db import get_conn
    with get_conn() as conn:
        token = get_token(conn)
        if not token:
            logger.error("Không có token trong pa_fb_tokens")
            sys.exit(1)

        account_ids = get_all_account_ids(conn)
        logger.info(f"Syncing insights cho {len(account_ids)} TK, {days} ngày...")

        data = batch_insights(token, account_ids, days=days)
        active = sum(1 for v in data.values() if v)
        logger.info(f"  {active} TK có spend")

        n = upsert_insights(conn, data)
        logger.info(f"  Upserted {n} rows vào pa_account_insights")

    logger.info("=== DONE ===")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=3)
    args = p.parse_args()
    main(days=args.days)
