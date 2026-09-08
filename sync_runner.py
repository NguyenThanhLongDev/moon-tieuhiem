import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_notify import broadcast_send_text
from pancake_auth import get_api_key as _get_api_key

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "sync_pos.log"


def log(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def send_telegram(text: str) -> None:
    try:
        broadcast_send_text(text)
    except Exception as e:
        log(f"ERROR send telegram: {e}")


# ===== LOAD CONFIG =====
try:
    import sys as _sys
    _sys.path.insert(0, str(BASE_DIR))
    from shop_helpers import load_all_shops as _load_shops
    shops = _load_shops()
except Exception:
    with open(BASE_DIR / "shops.json", "r", encoding="utf-8") as f:
        shops = json.load(f)

# ===== TIME RANGE =====
now = datetime.now(timezone.utc)
since_dt = now - timedelta(days=30)

since = since_dt.strftime("%Y-%m-%dT00:00:00.000Z")
until = now.strftime("%Y-%m-%dT23:59:59.999Z")

log("===== START SYNC =====")
log(f"Range: {since} -> {until}")

success_count = 0
fail_count = 0
failed_shops = []

for shop in shops:
    if shop.get("status") != "active":
        continue

    shop_key = shop["shop_key"]
    shop_name = shop["shop_name"]
    shop_id = shop["shop_id"]

    try:
        log(f"SYNC: {shop_name} ({shop_id})")

        url = f"https://pancake.vn/api/v1/shops/{shop_id}/report"

        headers = {
            "Authorization": f"Bearer {_get_api_key()}",
            "Content-Type": "application/json",
        }

        payload = {
            "params": {
                "returned_record": "success_record",
                "success_record": "inserted_at",
                "success_status": 1,
                "user_type": "update",
                "since": since,
                "until": until,
                "split_by": ["Time.day"],
                "select_fields": [
                    "cod",
                    "prepaid",
                    "partner_fee",
                    "total_order_count",
                    "order_count",
                    "order_count_pagination",
                    "shipping_fee",
                    "ads_amount",
                    "exchange_order_count",
                    "price",
                    "exchange_payment",
                    "discount",
                    "capital",
                    "surcharge",
                    "fee_marketplace",
                    "affiliate_price",
                    "marketplace_voucher",
                    "diff_shipping_fee",
                    "prepaid_by_point",
                ],
                "render_fields": [
                    "ads_amount",
                    "success_order_count",
                    "returned_order_count",
                    "price",
                    "discount",
                    "shipping_fee",
                    "partner_fee",
                    "capital",
                    "revenue",
                    "profit",
                    "sales",
                    "ads_order",
                    "avg_profit",
                ],
                "sorter": {"order": "descend", "field": "Time.day"},
                "pagination": {"pageSize": 100, "current": 1},
                "filter": {},
                "id": "sync-runner",
            }
        }

        res = requests.post(url, json=payload, headers=headers, timeout=30)

        if res.status_code != 200:
            raise Exception(f"HTTP {res.status_code}: {res.text[:300]}")

        data = res.json()

        output_file = BASE_DIR / f"data_{shop_key}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        log(f"OK: {shop_name}")
        success_count += 1

    except Exception as e:
        err = str(e)
        log(f"ERROR: {shop_name} -> {err}")
        fail_count += 1
        failed_shops.append(f"- {shop_name} ({shop_key}): {err[:120]}")

log("===== DONE =====")
log(f"SUCCESS: {success_count}")
log(f"FAIL: {fail_count}")

# ===== TELEGRAM ALERT =====
if fail_count > 0:
    msg = (
        "⚠️ SYNC POS LỖI\n"
        f"Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Thành công: {success_count}\n"
        f"Thất bại: {fail_count}\n\n"
        "Shop lỗi:\n"
        + "\n".join(failed_shops[:15])
    )
    send_telegram(msg)
else:
    log("Không có lỗi, không gửi cảnh báo Telegram.")
