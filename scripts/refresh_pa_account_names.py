#!/usr/bin/env python3
"""Cron hàng ngày — refresh tên TK QC trong pa_ad_accounts qua FB Graph trực tiếp.

Lý do: `_sync_all` dùng `/me/businesses` chỉ trả BMs USER owns, không trả BMs
system_user được assigned → 0 BM → không sync. Script này gọi `/act_<id>` cho
từng TK đã biết → cập nhật tên kể cả khi NV đổi tên TK trên FB.

Cron line:
    15 3 * * * cd /home/admin1/tieuhiemsoft/posbottieuhiem && \\
        .venv/bin/python scripts/refresh_pa_account_names.py >> logs/refresh_acct_names.log 2>&1
"""
from __future__ import annotations
import os
import sys
import requests
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

env_file = os.path.join(ROOT, "deploy", "pos-dashboard.env")
if os.path.isfile(env_file) and not os.environ.get("DATABASE_URL"):
    for line in open(env_file):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from db import get_conn


def main():
    print(f"=== refresh_pa_account_names @ {datetime.now().isoformat()} ===")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT access_token FROM pa_fb_tokens ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                print("FATAL: no token in pa_fb_tokens")
                sys.exit(1)
            token = row[0]
            cur.execute("SELECT account_id FROM pa_ad_accounts ORDER BY account_id")
            all_ids = [r[0] for r in cur.fetchall()]

    print(f"Refreshing {len(all_ids)} TK QC...")
    updated = unchanged = failed = 0
    session = requests.Session()
    with get_conn() as conn:
        with conn.cursor() as cur:
            for i, aid in enumerate(all_ids):
                try:
                    r = session.get(
                        f"https://graph.facebook.com/v20.0/{aid}",
                        params={"access_token": token, "fields": "name,account_status"},
                        timeout=10,
                    )
                    d = r.json()
                    if "error" in d:
                        failed += 1
                        continue
                    new_name = d.get("name", "")
                    status = d.get("account_status", 0)
                    cur.execute("SELECT account_name FROM pa_ad_accounts WHERE account_id=%s", (aid,))
                    old = cur.fetchone()
                    old_name = old[0] if old else ""
                    if new_name and new_name != old_name:
                        cur.execute("""UPDATE pa_ad_accounts
                                          SET account_name=%s, account_status=%s, synced_at=NOW()
                                        WHERE account_id=%s""", (new_name, status, aid))
                        updated += 1
                    else:
                        cur.execute("UPDATE pa_ad_accounts SET synced_at=NOW() WHERE account_id=%s", (aid,))
                        unchanged += 1
                except Exception:
                    failed += 1
                if i % 100 == 99:
                    conn.commit()
            conn.commit()
    print(f"DONE: {updated} updated, {unchanged} unchanged, {failed} failed")


if __name__ == "__main__":
    main()
