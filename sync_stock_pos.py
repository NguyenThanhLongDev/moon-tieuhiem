import json
import os
import requests
from datetime import datetime

BASE_URL = "https://pos.pages.fm/api/v1"


def load_shops():
    try:
        from shop_helpers import load_all_shops
        return load_all_shops()
    except Exception:
        with open("shops.json", "r", encoding="utf-8") as f:
            return json.load(f)


def load_shop_api_keys() -> dict:
    """Đọc per-shop api_key hex hợp lệ từ wh_shops (DB). Trả về {shop_id: api_key}."""
    try:
        from pancake_auth import get_shop_api_key, is_valid_hex_api_key, _load_shop_keys
        return _load_shop_keys()
    except Exception:
        pass
    try:
        import psycopg2
        db_url = os.environ.get("DATABASE_URL", "").strip()
        if not db_url:
            return {}
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute(
            "SELECT pos_shop_id, pos_api_key FROM wh_shops "
            "WHERE pos_api_key IS NOT NULL AND pos_api_key != '' AND status='active'"
        )
        result = {}
        for r in cur.fetchall():
            sid = str(r[0] or "").strip()
            key = str(r[1] or "").strip()
            if sid and len(key) == 32 and all(c in "0123456789abcdefABCDEF" for c in key):
                result[sid] = key
        cur.close()
        conn.close()
        return result
    except Exception as exc:
        print(f"WARN: Không load được api_key từ DB: {exc}")
        return {}


def fetch_products(shop, access_token):
    url = f"{BASE_URL}/shops/{shop['shop_id']}/products"
    headers = {
        "Content-Type": "application/json",
    }
    params = {
        "page": 1,
        "limit": 200,
        "api_key": access_token,
    }

    r = requests.get(url, headers=headers, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def save_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    shops = load_shops()
    shop_api_keys = load_shop_api_keys()
    today = datetime.now().strftime("%Y-%m-%d")

    synced = 0
    for shop in shops:
        if shop.get("status") != "active":
            continue

        shop_id = str(shop.get("shop_id", "")).strip()
        api_key = shop_api_keys.get(shop_id, "")
        if not api_key:
            print(f"SKIP {shop.get('shop_name')} ({shop_id}): chưa có api_key hợp lệ")
            continue

        try:
            data = fetch_products(shop, api_key)

            latest_file = f"stock_{shop_id}.json"
            history_file = f"stock_history/{today}/shop_{shop_id}.json"

            save_json(latest_file, data)
            save_json(history_file, data)

            print(f"OK {shop.get('shop_name')} | shop_id {shop_id} -> {latest_file} | {history_file}")
            synced += 1
        except Exception as e:
            print(f"ERROR {shop.get('shop_name')}: {e}")

    print(f"=== Stock sync done: {synced}/{len(shops)} shops ===")


if __name__ == "__main__":
    main()
