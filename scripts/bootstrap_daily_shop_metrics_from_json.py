from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402


DATA_GLOBS = [
    str(BASE_DIR / "data_shop*.json"),
    str(BASE_DIR / "data" / "data_shop*.json"),
]


def discover_files() -> List[str]:
    files: List[str] = []
    for pattern in DATA_GLOBS:
        files.extend(glob.glob(pattern))
    return sorted(set(files))


def normalize_date_value(value: Any) -> str:
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    if re.match(r"^\d{2}/\d{2}/\d{4}$", text):
        d, m, y = text.split("/")
        return f"{y}-{m}-{d}"
    if re.match(r"^\d{2}-\d{2}-\d{4}$", text):
        d, m, y = text.split("-")
        return f"{y}-{m}-{d}"
    return text


def extract_shop_key_from_filename(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0].replace("data_", "")


def _safe_float_metric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def parse_rows(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        full_data = json.load(f)
    rows = full_data.get("data", []) if isinstance(full_data, dict) else []
    if not isinstance(rows, list):
        return []
    results: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = normalize_date_value(row.get("Time.day", ""))
        if not day:
            continue
        result = row.get("result", {}) if isinstance(row.get("result", {}), dict) else {}
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            continue
        results.append({
            "metric_date": day,
            "order_count": int(float(result.get("order_count", 0) or 0)),
            "total_order_count": int(float(result.get("total_order_count", 0) or 0)),
            "confirmed_count": int(float(result.get("success_order_count", 0) or 0)),
            "returned_count": int(float(result.get("returned_order_count", 0) or 0)),
            "gross_revenue": float(result.get("revenue", 0) or 0),
            "net_revenue": float(result.get("revenue", 0) or 0),
            "ads_cost": float(result.get("ads_amount", 0) or 0),
            "pos_profit_loss": float(result.get("profit", 0) or 0),
            "pos_avg_profit_per_order": _safe_float_metric(result.get("avg_profit")),
        })
    return results


def _zero_metric_row(metric_date: str) -> Dict[str, Any]:
    """Dòng 0 cho ngày POS không trả về (không có đơn + không có ads)."""
    return {
        "metric_date": metric_date,
        "order_count": 0,
        "total_order_count": 0,
        "confirmed_count": 0,
        "returned_count": 0,
        "gross_revenue": 0.0,
        "net_revenue": 0.0,
        "ads_cost": 0.0,
        "pos_profit_loss": 0.0,
        "pos_avg_profit_per_order": 0.0,
    }


def fill_missing_days_with_zero(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lấp các ngày POS bỏ trống TRONG khoảng [min, max] của file bằng dòng 0.

    POS analytics bỏ qua ngày không hoạt động (0 đơn + 0 ads) → ngày đó biến mất
    khỏi data_shop*.json. Nếu trước đó ngày này từng có số (vd ads), giá trị cũ sẽ
    KẸT LẠI trong daily_shop_metrics vì bootstrap chỉ ghi ngày có trong file.
    Lấp bằng 0 để "lợi nhuận POS = 0 thì DB = 0" (không lấy phí ads Meta thay thế).
    Chỉ lấp khoảng trống bên trong [min, max]; KHÔNG ghi đè ngày đã có dữ liệu thật.
    """
    if not rows:
        return rows
    present: Set[str] = {r["metric_date"] for r in rows}
    sorted_dates = sorted(present)
    try:
        start = datetime.strptime(sorted_dates[0], "%Y-%m-%d").date()
        end = datetime.strptime(sorted_dates[-1], "%Y-%m-%d").date()
    except ValueError:
        return rows
    out = list(rows)
    cursor: date = start
    while cursor <= end:
        ds = cursor.isoformat()
        if ds not in present:
            out.append(_zero_metric_row(ds))
        cursor += timedelta(days=1)
    return out


def metric_dates_in_file(path: str) -> Set[str]:
    return {r["metric_date"] for r in parse_rows(path)}


def ensure_pos_json_contains_date(require_date: str) -> None:
    """If any shop JSON lacks this calendar day, run sync_pos.py --date (POS re-fetch, merge by day)."""
    try:
        datetime.strptime(require_date, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"Invalid --require-date (expected YYYY-MM-DD): {require_date!r}")
    files = discover_files()
    missing = False
    for path in files:
        if require_date not in metric_dates_in_file(path):
            missing = True
            break
    if not missing:
        return
    sync_script = BASE_DIR / "sync_pos.py"
    if not sync_script.is_file():
        raise SystemExit(f"sync_pos.py not found at {sync_script}")
    subprocess.run(
        [sys.executable, str(sync_script), "--date", require_date],
        cwd=str(BASE_DIR),
        check=True,
        env=os.environ.copy(),
    )


def load_shop_key_to_id(cur) -> Dict[str, int]:
    cur.execute("SELECT id, shop_key FROM shops WHERE shop_key IS NOT NULL")
    result: Dict[str, int] = {}
    for row in cur.fetchall():
        shop_id, shop_key = row
        result[str(shop_key)] = int(shop_id)
    return result


def upsert_daily_metrics(cur, shop_id: int, row: Dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO daily_shop_metrics (
            shop_id, metric_date,
            order_count, total_order_count, new_order_count, confirmed_count, shipping_count, delivered_count, returned_count, cancelled_count,
            gross_revenue, net_revenue, ads_cost, pos_profit_loss, pos_avg_profit_per_order, loss_flag, inventory_value
        )
        VALUES (
            %s, %s,
            %s, %s, 0, %s, 0, 0, %s, 0,
            %s, %s, %s, %s, %s, %s, 0
        )
        ON CONFLICT (shop_id, metric_date)
        DO UPDATE SET
            order_count = EXCLUDED.order_count,
            total_order_count = EXCLUDED.total_order_count,
            confirmed_count = EXCLUDED.confirmed_count,
            returned_count = EXCLUDED.returned_count,
            gross_revenue = EXCLUDED.gross_revenue,
            net_revenue = EXCLUDED.net_revenue,
            ads_cost = EXCLUDED.ads_cost,
            pos_profit_loss = EXCLUDED.pos_profit_loss,
            pos_avg_profit_per_order = EXCLUDED.pos_avg_profit_per_order,
            loss_flag = EXCLUDED.loss_flag
        """,
        (
            shop_id,
            row["metric_date"],
            row["order_count"],
            row["total_order_count"],
            row["confirmed_count"],
            row["returned_count"],
            row["gross_revenue"],
            row["net_revenue"],
            row["ads_cost"],
            row["pos_profit_loss"],
            row["pos_avg_profit_per_order"],
            row["pos_profit_loss"] < 0,
        ),
    )


def bootstrap_daily_metrics(require_date: str = "") -> Tuple[int, int]:
    if require_date.strip():
        ensure_pos_json_contains_date(require_date.strip())
    files = discover_files()
    upsert_count = 0
    skipped_count = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            shop_key_to_id = load_shop_key_to_id(cur)
            for path in files:
                shop_key = extract_shop_key_from_filename(path)
                shop_id = shop_key_to_id.get(shop_key)
                if not shop_id:
                    skipped_count += 1
                    continue
                for row in fill_missing_days_with_zero(parse_rows(path)):
                    upsert_daily_metrics(cur, shop_id=shop_id, row=row)
                    upsert_count += 1
    return upsert_count, skipped_count


if __name__ == "__main__":
    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required. Example: export DATABASE_URL=postgresql://user:pass@host:5432/db")
    ap = argparse.ArgumentParser(description="Bootstrap daily_shop_metrics from POS analytics JSON.")
    ap.add_argument(
        "--require-date",
        default="",
        help="YYYY-MM-DD: re-sync that day from POS (merge JSON) if any shop file is missing it, then bootstrap.",
    )
    ns = ap.parse_args()
    upsert_count, skipped_count = bootstrap_daily_metrics(require_date=ns.require_date)
    print(f"Daily metrics bootstrap done. upserts={upsert_count}, skipped_files_without_shop_mapping={skipped_count}")
