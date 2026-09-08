#!/usr/bin/env python3
"""READ-ONLY: đếm trực tiếp từ Pancake POS số đơn ĐVVC đã lấy (status_history[2]) trong 1 ngày.

KHÔNG ghi DB. Chỉ gọi API + đếm. Dùng để đối chiếu với con số phần mềm hiển thị.

Cách đếm = đúng bộ lọc 'ĐVVC lấy hàng' của Pancake: status_history có entry
status=2 với updated_at rơi vào ngày target (giờ VN +7).

Fetch status 2 (đang giao) + status 3 (đã nhận) để không sót đơn ship hôm nay
đã giao luôn trong ngày. Full fetch, KHÔNG early-terminate → không sót đơn cũ.

Usage: python scripts/count_pos_shipped_live.py 2026-05-22
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from db import query_all
from modules.kho_vat_ly.wh_sync_orders import fetch_orders_for_shop, _safe_parse_dt


def _date_entered_status(order, status_code):
    """Ngày đơn VÀO 1 status (status_history[status].updated_at, giờ VN) dạng YYYY-MM-DD."""
    for h in order.get("status_history") or []:
        if str(h.get("status", "")) == str(status_code):
            dt = _safe_parse_dt(h.get("updated_at"))
            if dt:
                return dt.strftime("%Y-%m-%d")
    return None


def pickup_date(order):
    """Ngày ĐVVC lấy = vào status 2 (Shipped)."""
    return _date_entered_status(order, 2)


def push_date(order):
    """Ngày kho/shop đẩy lên ĐVVC = vào status 9 (Waiting for pick up)."""
    return _date_entered_status(order, 9)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        print("Thiếu ngày target. VD: 2026-05-22")
        sys.exit(1)

    rows = query_all(
        "SELECT shop_name, pos_shop_id, pos_api_key FROM wh_shops "
        "WHERE status='active' AND pos_api_key IS NOT NULL AND pos_api_key<>''"
    )
    shops = [{"shop_name": r[0], "pos_shop_id": r[1], "pos_api_key": r[2]} for r in rows]

    # Đơn vào status 2 (ĐVVC lấy) ngày target có thể ĐÃ chạy tiếp sang trạng thái khác.
    # Phải kéo MỌI trạng thái sau-khi-lấy, rồi soi status_history mốc status=2.
    #   2=Shipped 3=Received 16=Collected money 4=Returning 15=Partial return
    #   5=Returned 6=Canceled 7=Deleted recently
    # Thêm 9 (chờ lấy) để bắt đơn kho đẩy lên ĐVVC hôm nay nhưng shipper chưa lấy.
    POST_PICKUP_STATUSES = (9, 2, 3, 16, 4, 15, 5, 6, 7)
    tasks = []
    for s in shops:
        for status in POST_PICKUP_STATUSES:
            tasks.append((s, status))

    # dedup theo (shop_name, order_id)
    pickup_keys = set()   # vào status 2 ngày target = ĐVVC đã lấy
    push_keys = set()     # vào status 9 ngày target = kho đẩy lên ĐVVC
    errors = []

    from datetime import date, timedelta
    s_from = (date.today() - timedelta(days=14)).strftime("%Y-%m-%d")

    def work(task):
        s, status = task
        orders, err = fetch_orders_for_shop(
            str(s["pos_shop_id"]), api_key=str(s["pos_api_key"]),
            date_from=("" if status == 2 else s_from), date_to="",
            pancake_status=status,
            cursor_inserted_at="",  # full fetch (status 2) / early-stop theo date_from (còn lại)
        )
        pk, ph = [], []
        for o in orders or []:
            code = str(o.get("id") or "")
            if pickup_date(o) == target:
                pk.append(code)
            if push_date(o) == target:
                ph.append(code)
        return s["shop_name"], status, pk, ph, err

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(work, t): t for t in tasks}
        for fut in as_completed(futs):
            name, status, pk, ph, err = fut.result()
            if err:
                errors.append(f"{name} (status {status}): {err}")
                continue
            for code in pk:
                pickup_keys.add((name, code))
            for code in ph:
                push_keys.add((name, code))

    print(f"\n=== POS LIVE ngày {target} ===")
    print(f"Vào status 9 (kho đẩy lên ĐVVC):       {len(push_keys)}")
    print(f"Vào status 2 (ĐVVC đã quét lấy):        {len(pickup_keys)}")
    print(f"Đẩy lên nhưng shipper CHƯA lấy (9 - 2 giao nhau): "
          f"{len(push_keys - pickup_keys)}")
    print(f"\nShop fetch lỗi: {len(errors)}")
    for e in errors[:15]:
        print("  ERR:", e)


if __name__ == "__main__":
    main()
