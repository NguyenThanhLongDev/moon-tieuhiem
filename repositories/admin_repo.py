from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def upsert_team(cur, team_code: str, team_name: str, status: str = "active") -> int:
    cur.execute(
        """
        INSERT INTO teams (team_code, team_name, status)
        VALUES (%s, %s, %s)
        ON CONFLICT (team_code)
        DO UPDATE SET team_name = EXCLUDED.team_name, status = EXCLUDED.status
        RETURNING id
        """,
        (team_code, team_name, status),
    )
    row = cur.fetchone()
    return int(row[0])


def get_team_id_by_code(cur, team_code: str) -> Optional[int]:
    cur.execute("SELECT id FROM teams WHERE team_code = %s", (team_code,))
    row = cur.fetchone()
    return int(row[0]) if row else None


def upsert_user(
    cur,
    username: str,
    password_hash: str,
    role: str,
    team_id: Optional[int],
    status: str = "active",
) -> int:
    cur.execute(
        """
        INSERT INTO users (username, password_hash, role, team_id, status)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (username)
        DO UPDATE SET
            password_hash = EXCLUDED.password_hash,
            role = EXCLUDED.role,
            team_id = EXCLUDED.team_id,
            status = EXCLUDED.status
        RETURNING id
        """,
        (username, password_hash, role, team_id, status),
    )
    row = cur.fetchone()
    return int(row[0])


def upsert_shop(
    cur,
    shop_key: str,
    shop_name: str,
    shop_code: Optional[str],
    team_id: Optional[int],
    status: str,
) -> int:
    cur.execute(
        """
        INSERT INTO shops (shop_key, shop_name, shop_code, team_id, status)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (shop_key)
        DO UPDATE SET
            shop_name = EXCLUDED.shop_name,
            shop_code = EXCLUDED.shop_code,
            team_id = EXCLUDED.team_id,
            status = EXCLUDED.status
        RETURNING id
        """,
        (shop_key, shop_name, shop_code, team_id, status),
    )
    row = cur.fetchone()
    return int(row[0])


def upsert_user_shop_assignment(
    cur, user_id: int, shop_id: int, assigned_by: Optional[int],
    assigned_from: Optional[str] = None, reason: Optional[str] = None,
) -> None:
    """Gán shop cho NV (versioned, migration 058 — giống assign_account_to_user của TK QC).

    `assigned_from` (YYYY-MM-DD, mặc định hôm nay) = ngày hiệu lực. Holder khác đang
    giữ shop sẽ bị đóng hiệu lực ngày trước đó — lịch sử giữ nguyên cho báo cáo/lương
    theo kỳ. Idempotent: NV đã đang giữ shop thì không làm gì.
    """
    cur.execute(
        "SELECT 1 FROM user_shop_assignments"
        " WHERE user_id = %s AND shop_id = %s AND assigned_to IS NULL",
        (user_id, shop_id),
    )
    if cur.fetchone():
        return
    # Đóng holder khác: gán từ ngày hiệu lực trở đi → xóa (gán nhầm); trước đó → đóng
    cur.execute(
        """
        DELETE FROM user_shop_assignments
        WHERE shop_id = %s AND assigned_to IS NULL
          AND assigned_from >= COALESCE(%s::date, CURRENT_DATE)
        """,
        (shop_id, assigned_from),
    )
    cur.execute(
        """
        UPDATE user_shop_assignments
           SET assigned_to = COALESCE(%s::date, CURRENT_DATE) - 1
         WHERE shop_id = %s AND assigned_to IS NULL
        """,
        (assigned_from, shop_id),
    )
    cur.execute(
        """
        INSERT INTO user_shop_assignments (user_id, shop_id, assigned_by, assigned_from, reason)
        VALUES (%s, %s, %s, COALESCE(%s::date, CURRENT_DATE), %s)
        ON CONFLICT DO NOTHING
        """,
        (user_id, shop_id, assigned_by, assigned_from, reason),
    )


def upsert_web(cur, shop_id: int, web_name: str, domain: Optional[str], status: str) -> None:
    cur.execute(
        """
        INSERT INTO webs (shop_id, web_name, domain, status)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (shop_id, web_name, domain, status),
    )


def get_shop_id_by_key(cur, shop_key: str) -> Optional[int]:
    cur.execute("SELECT id FROM shops WHERE shop_key = %s", (shop_key,))
    row = cur.fetchone()
    return int(row[0]) if row else None


def upsert_fb_ad_account_mapping(
    cur,
    shop_id: int,
    fb_ad_account_id: str,
    account_name: str,
    status: str = "active",
    assigned_from: Optional[str] = None,
) -> int:
    # assigned_from (YYYY-MM-DD): ngày hiệu lực gán shop. None → CURRENT_DATE.
    # Cho phép backdate để báo cáo ngày cũ thấy shop/NV (gán muộn).
    cur.execute(
        """
        INSERT INTO fb_ad_account_mappings (shop_id, fb_ad_account_id, account_name, status, assigned_from)
        VALUES (%s, %s, %s, %s, COALESCE(%s::date, CURRENT_DATE))
        ON CONFLICT (shop_id, fb_ad_account_id) WHERE assigned_to IS NULL
        DO UPDATE SET
            account_name = EXCLUDED.account_name,
            status = EXCLUDED.status,
            assigned_from = COALESCE(%s::date, fb_ad_account_mappings.assigned_from)
        RETURNING id, (xmax = 0) AS is_new
        """,
        (shop_id, fb_ad_account_id, account_name, status, assigned_from, assigned_from),
    )
    row = cur.fetchone()
    mapping_id = int(row[0])
    is_new = bool(row[1])  # True nếu vừa INSERT (chưa từng có), False nếu UPDATE existing

    # Trigger backfill 7 ngày ngược (background, không block request)
    # Chỉ trigger khi mapping mới hoàn toàn — tránh chạy lại khi admin toggle status.
    if is_new:
        _trigger_fb_ads_backfill_async(fb_ad_account_id, days_back=7)

    return mapping_id


def _trigger_fb_ads_backfill_async(fb_ad_account_id: str, days_back: int = 7) -> None:
    """Spawn background subprocess backfill cho ad account vừa thêm.

    Không block caller. Lỗi log warning, không raise.
    """
    import subprocess
    import threading
    import datetime as _dt
    import logging
    from pathlib import Path

    log = logging.getLogger(__name__)

    def _worker():
        try:
            today = _dt.date.today()
            date_from = today - _dt.timedelta(days=days_back)
            base = Path(__file__).resolve().parents[1]
            env = {**os.environ}
            python = str(base / ".venv" / "bin" / "python3")
            for script in ("scripts/sync_fb_ads_by_page.py", "scripts/sync_facebook_ads_to_db.py"):
                cmd = [
                    python, str(base / script),
                    "--date-from", date_from.strftime("%Y-%m-%d"),
                    "--date-to", today.strftime("%Y-%m-%d"),
                    "--fb-ad-account-id", fb_ad_account_id,
                ]
                log.info("[fb_backfill] %s for %s", script, fb_ad_account_id)
                subprocess.run(cmd, cwd=str(base), env=env, timeout=600, check=False)
        except Exception as exc:
            log.warning("[fb_backfill] error: %s", exc)

    threading.Thread(target=_worker, daemon=True, name=f"fb-backfill-{fb_ad_account_id[:10]}").start()


def toggle_fb_ad_account_mapping_status(cur, mapping_id: int) -> None:
    cur.execute(
        """
        UPDATE fb_ad_account_mappings
        SET status = CASE WHEN status = 'active' THEN 'inactive' ELSE 'active' END
        WHERE id = %s
        """,
        (mapping_id,),
    )


def delete_fb_ad_account_mapping(cur, mapping_id: int) -> None:
    cur.execute("DELETE FROM fb_ad_account_mappings WHERE id = %s", (mapping_id,))


def list_fb_ad_account_mappings(cur, active_only: bool = False) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            m.id,
            s.shop_key,
            s.shop_name,
            m.fb_ad_account_id,
            m.account_name,
            m.status,
            m.updated_at
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
    """
    params: List[Any] = []
    if active_only:
        sql += " WHERE m.status = %s"
        params.append("active")
    sql += " ORDER BY s.shop_name, m.fb_ad_account_id"
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        result.append({
            "id": int(row[0]),
            "shop_key": str(row[1] or ""),
            "shop_name": str(row[2] or ""),
            "fb_ad_account_id": str(row[3] or ""),
            "account_name": str(row[4] or ""),
            "status": str(row[5] or ""),
            "updated_at": row[6],
        })
    return result


def get_fb_ad_account_mappings(
    cur,
    shop_key: Optional[str] = None,
    fb_ad_account_id: Optional[str] = None,
    active_only: bool = False,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            m.id,
            m.shop_id,
            s.shop_key,
            s.shop_name,
            m.fb_ad_account_id,
            m.account_name,
            m.status
        FROM fb_ad_account_mappings m
        JOIN shops s ON s.id = m.shop_id
        WHERE 1=1
    """
    params: List[Any] = []
    if shop_key:
        sql += " AND s.shop_key = %s"
        params.append(shop_key)
    if fb_ad_account_id:
        sql += " AND m.fb_ad_account_id = %s"
        params.append(fb_ad_account_id)
    if active_only:
        sql += " AND m.status = %s"
        params.append("active")
    sql += " ORDER BY s.shop_name, m.fb_ad_account_id"
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        result.append({
            "id": int(row[0]),
            "shop_id": int(row[1]),
            "shop_key": str(row[2] or ""),
            "shop_name": str(row[3] or ""),
            "fb_ad_account_id": str(row[4] or ""),
            "account_name": str(row[5] or ""),
            "status": str(row[6] or ""),
        })
    return result


## ═══════════════════════════════════════════════════════════════════
## Versioned NV ↔ TK QC assignment (migration 036)
## ═══════════════════════════════════════════════════════════════════

def assign_account_to_user(
    cur,
    ad_account_id: str,
    account_name: str,
    user_id: int,
    assigned_by: Optional[int],
    reason: Optional[str] = None,
    effective_date: Optional[str] = None,
    shop_ids: Optional[List[int]] = None,
) -> int:
    """Gán TK QC cho NV. Close phụ trách cũ (nếu có), mở khoảng mới từ `effective_date` (default = today).

    `shop_ids`:
      - None → KHÔNG đụng đến shop mappings (giữ nguyên — dùng khi chỉ đổi NV không đổi shop).
      - []   → close hết shop mappings active của TK (TK chưa biết chạy shop nào).
      - [..] → close hết + mở rows cho từng shop trong list (phải nằm trong assigned_shops của NV).

    Trả về id của row uaa mới.
    """
    # ⚠ FIX 2026-05-14: close uaa active cũ TRƯỚC khi INSERT để tránh vi phạm
    # partial unique `uq_uaa_active (ad_account_id) WHERE assigned_to IS NULL`.
    # ⚠ FIX 2026-06-21: nếu khoảng active cũ bắt đầu >= effective_date thì
    # close với `effective_date - 1` sẽ ra window âm (assigned_to < assigned_from)
    # → vỡ chk_uaa_window. Đây là gán nhầm/cùng ngày → xóa hẳn row cũ rồi mới
    # close phần còn lại (giống pattern user_shop_assignments).
    cur.execute(
        """
        DELETE FROM user_ad_account_assignments
         WHERE ad_account_id = %s
           AND assigned_to IS NULL
           AND assigned_from >= COALESCE(%s::date, CURRENT_DATE)
        """,
        (ad_account_id, effective_date),
    )
    cur.execute(
        """
        UPDATE user_ad_account_assignments
           SET assigned_to = (COALESCE(%s::date, CURRENT_DATE) - INTERVAL '1 day')::date
         WHERE ad_account_id = %s
           AND assigned_to IS NULL
        """,
        (effective_date, ad_account_id),
    )
    cur.execute(
        """
        INSERT INTO user_ad_account_assignments
            (user_id, ad_account_id, ad_account_name, assigned_from, assigned_by, reason)
        VALUES (%s, %s, %s, COALESCE(%s::date, CURRENT_DATE), %s, %s)
        RETURNING id, assigned_from
        """,
        (user_id, ad_account_id, account_name, effective_date, assigned_by, reason),
    )
    row = cur.fetchone()
    new_assignment_id = int(row[0])
    eff_date = row[1]

    # Shop mappings: chỉ đụng nếu shop_ids được truyền
    if shop_ids is not None:
        set_shop_mappings_for_account(cur, ad_account_id, account_name, user_id, shop_ids, eff_date)

    return new_assignment_id


def set_shop_mappings_for_account(
    cur,
    ad_account_id: str,
    account_name: str,
    user_id: Optional[int],
    shop_ids: List[int],
    effective_date: Optional[str] = None,
) -> Dict[str, int]:
    """Set tường minh danh sách shop của 1 TK QC.

    Close mọi mapping active hiện tại của TK + open mappings mới cho `shop_ids`.
    `shop_ids = []` → chỉ close, không open (TK chưa biết shop).
    """
    cur.execute(
        """
        UPDATE fb_ad_account_mappings
           SET assigned_to = COALESCE(%s::date, CURRENT_DATE),
               status      = 'inactive'
         WHERE fb_ad_account_id = %s
           AND assigned_to IS NULL
        """,
        (effective_date, ad_account_id),
    )
    closed = cur.rowcount

    opened = 0
    for shop_id in shop_ids or []:
        cur.execute(
            """
            INSERT INTO fb_ad_account_mappings
                (shop_id, fb_ad_account_id, account_name, status,
                 assigned_from, assigned_to, derived_from_user_id)
            VALUES (%s, %s, %s, 'active', COALESCE(%s::date, CURRENT_DATE), NULL, %s)
            """,
            (int(shop_id), ad_account_id, account_name, effective_date, user_id),
        )
        opened += 1

    return {"closed": closed, "opened": opened}


def auto_map_single_shop_accounts(cur) -> int:
    """Tự nối TK QC → shop POS cho NV có ĐÚNG 1 shop active.

    Mục đích: TK gán cho NV (qua user_ad_account_assignments active) nhưng chưa map
    shop POS → báo cáo hiện "Chưa gán shop" dù NV có shop. Hàm này tự tạo mapping khi
    KHÔNG nhập nhằng (NV chỉ có 1 shop active). NV nhiều shop / không shop → bỏ qua
    (để map tay, tránh gán sai shop). Idempotent: chỉ map TK chưa có mapping active.

    Trả về số mapping vừa tạo.
    """
    cur.execute(
        """
        WITH nv1 AS (
            SELECT usa.user_id, MIN(s.id) AS shop_id
              FROM user_shop_assignments usa
              JOIN shops s ON s.id = usa.shop_id
             WHERE s.status = 'active' AND usa.assigned_to IS NULL
             GROUP BY usa.user_id
            HAVING COUNT(DISTINCT s.id) = 1
        ),
        unmapped AS (
            SELECT DISTINCT a.ad_account_id, a.user_id,
                   COALESCE(NULLIF(a.ad_account_name, ''), a.ad_account_id) AS account_name
              FROM user_ad_account_assignments a
             WHERE a.assigned_to IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM fb_ad_account_mappings m
                    WHERE m.fb_ad_account_id = a.ad_account_id AND m.status = 'active')
        )
        INSERT INTO fb_ad_account_mappings
            (shop_id, fb_ad_account_id, account_name, status,
             assigned_from, assigned_to, derived_from_user_id)
        SELECT n.shop_id, u.ad_account_id, u.account_name, 'active',
               CURRENT_DATE, NULL, u.user_id
          FROM unmapped u
          JOIN nv1 n ON n.user_id = u.user_id
        """
    )
    return cur.rowcount


def unassign_account(
    cur,
    ad_account_id: str,
    reason: Optional[str] = None,
    effective_date: Optional[str] = None,
) -> int:
    """Đóng phụ trách hiện tại của TK QC (không gán cho NV mới).

    Close uaa active + shop mappings active. Trả về số row uaa đã close.
    """
    cur.execute(
        """
        UPDATE user_ad_account_assignments
           SET assigned_to = COALESCE(%s::date, CURRENT_DATE),
               reason      = COALESCE(%s, reason)
         WHERE ad_account_id = %s
           AND assigned_to IS NULL
         RETURNING id, assigned_to
        """,
        (effective_date, reason, ad_account_id),
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    eff_date = rows[0][1]
    _close_active_shop_mappings_for_account(cur, ad_account_id, eff_date)
    return len(rows)


def _close_active_shop_mappings_for_account(cur, ad_account_id: str, effective_date) -> int:
    """Đặt assigned_to=effective_date cho mọi shop mapping active của 1 TK."""
    cur.execute(
        """
        UPDATE fb_ad_account_mappings
           SET assigned_to = %s::date,
               status      = 'inactive'
         WHERE fb_ad_account_id = %s
           AND assigned_to IS NULL
         RETURNING id
        """,
        (effective_date, ad_account_id),
    )
    return cur.rowcount


def _open_shop_mappings_from_user(
    cur, ad_account_id: str, account_name: str, user_id: int, effective_date,
) -> int:
    """Sinh shop mappings active cho TK dựa trên user_shop_assignments của NV."""
    cur.execute(
        """
        INSERT INTO fb_ad_account_mappings
            (shop_id, fb_ad_account_id, account_name, status,
             assigned_from, assigned_to, derived_from_user_id)
        SELECT usa.shop_id, %s, %s, 'active', %s::date, NULL, %s
          FROM user_shop_assignments usa
         WHERE usa.user_id = %s AND usa.assigned_to IS NULL
           AND NOT EXISTS (
                SELECT 1 FROM fb_ad_account_mappings m
                 WHERE m.shop_id = usa.shop_id
                   AND m.fb_ad_account_id = %s
                   AND m.assigned_to IS NULL
           )
        RETURNING id
        """,
        (ad_account_id, account_name, effective_date, user_id, user_id, ad_account_id),
    )
    return cur.rowcount


def sync_shop_mappings_for_user(cur, user_id: int, effective_date: Optional[str] = None) -> Dict[str, int]:
    """Đồng bộ `fb_ad_account_mappings` khi `user_shop_assignments` của NV đổi.

    CHỈ close — KHÔNG auto-open: khi NV bị bỏ shop X, tất cả TK của NV đang map
    shop X sẽ close. Còn khi NV được thêm shop mới, IT phải vào trang TK QC tự
    pick shop nếu muốn TK đó chạy shop mới.

    Lý do (2026-05-12): 1 NV có thể có N shop nhưng mỗi TK chỉ chạy SUBSET shop.
    Auto-open all-of-NV's-shops sẽ sinh mapping sai cho TK chạy hẹp hơn.
    """
    closed = 0

    # Close: shop mappings active mà shop_id không còn trong assigned của NV
    cur.execute(
        """
        UPDATE fb_ad_account_mappings m
           SET assigned_to = COALESCE(%s::date, CURRENT_DATE),
               status      = 'inactive'
          FROM user_ad_account_assignments a
         WHERE a.user_id          = %s
           AND a.assigned_to      IS NULL
           AND m.fb_ad_account_id = a.ad_account_id
           AND m.assigned_to      IS NULL
           AND m.shop_id NOT IN (
                SELECT shop_id FROM user_shop_assignments
                WHERE user_id = %s AND assigned_to IS NULL
           )
        """,
        (effective_date, user_id, user_id),
    )
    closed = cur.rowcount

    return {"closed": closed, "opened": 0}


def get_active_assignment(cur, ad_account_id: str) -> Optional[Dict[str, Any]]:
    """Lấy thông tin NV đang phụ trách 1 TK QC (uaa active)."""
    cur.execute(
        """
        SELECT a.id, a.user_id, u.username, u.full_name,
               a.assigned_from, a.assigned_by, a.reason
          FROM user_ad_account_assignments a
          LEFT JOIN users u ON u.id = a.user_id
         WHERE a.ad_account_id = %s AND a.assigned_to IS NULL
         LIMIT 1
        """,
        (ad_account_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "assignment_id": int(row[0]),
        "user_id": int(row[1]),
        "username": row[2] or "",
        "full_name": row[3] or "",
        "assigned_from": row[4],
        "assigned_by": int(row[5]) if row[5] is not None else None,
        "reason": row[6] or "",
    }


def list_account_history(cur, ad_account_id: str) -> List[Dict[str, Any]]:
    """Danh sách lịch sử phụ trách của 1 TK QC, mới nhất trước."""
    cur.execute(
        """
        SELECT a.id, a.user_id, u.username, u.full_name,
               a.assigned_from, a.assigned_to, b.username AS by_username, a.reason, a.created_at
          FROM user_ad_account_assignments a
          LEFT JOIN users u ON u.id = a.user_id
          LEFT JOIN users b ON b.id = a.assigned_by
         WHERE a.ad_account_id = %s
         ORDER BY a.assigned_from DESC, a.id DESC
        """,
        (ad_account_id,),
    )
    return [
        {
            "id": int(r[0]),
            "user_id": int(r[1]),
            "username": r[2] or "",
            "full_name": r[3] or "",
            "assigned_from": r[4],
            "assigned_to": r[5],
            "assigned_by_username": r[6] or "",
            "reason": r[7] or "",
            "created_at": r[8],
        }
        for r in cur.fetchall()
    ]


def list_all_active_assignments(cur) -> Dict[str, Dict[str, Any]]:
    """Dict ad_account_id → {user_id, username, full_name} đang phụ trách."""
    cur.execute(
        """
        SELECT a.ad_account_id, a.user_id, u.username, u.full_name, a.assigned_from
          FROM user_ad_account_assignments a
          LEFT JOIN users u ON u.id = a.user_id
         WHERE a.assigned_to IS NULL
        """
    )
    result: Dict[str, Dict[str, Any]] = {}
    for r in cur.fetchall():
        result[str(r[0])] = {
            "user_id": int(r[1]),
            "username": r[2] or "",
            "full_name": r[3] or "",
            "assigned_from": r[4],
        }
    return result


## ═══════════════════════════════════════════════════════════════════
## Page ↔ POS shop binding (migration 037)
## ═══════════════════════════════════════════════════════════════════

def set_page_shop_binding(
    cur,
    page_id: str,
    pos_shop_id: Optional[int],
    assigned_by: Optional[int],
    reason: Optional[str] = None,
    note: Optional[str] = None,
    effective_date: Optional[str] = None,
) -> int:
    """Set page → POS shop. Nếu page đã có binding active thì close khoảng cũ
    + mở khoảng mới từ effective_date (default = today). pos_shop_id=None →
    mark page as test/zombie (đã review nhưng không thuộc shop nào).

    Trả về id row binding mới.
    """
    # ⚠ FIX 2026-05-14: close binding active cũ TRƯỚC khi INSERT mới.
    # Trước đây INSERT trước → 2 row có assigned_to=NULL cùng lúc → vi phạm
    # partial unique `uq_psb_active`. Phải close cũ trước.
    # Effective date của close = effective_date - 1 day (nếu khác hôm nay) HOẶC today-1.
    # Nếu effective_date trong tương lai, close cũ với today (giữ history liền mạch).
    # ⚠ FIX 2026-06-21: binding active cũ bắt đầu >= effective_date → close với
    # `effective_date - 1` ra window âm → vỡ chk_psb_window. Xóa hẳn row gán nhầm
    # rồi mới close phần còn lại.
    cur.execute(
        """
        DELETE FROM fb_page_shop_binding
         WHERE page_id = %s
           AND assigned_to IS NULL
           AND assigned_from >= COALESCE(%s::date, CURRENT_DATE)
        """,
        (page_id, effective_date),
    )
    cur.execute(
        """
        UPDATE fb_page_shop_binding
           SET assigned_to = (COALESCE(%s::date, CURRENT_DATE) - INTERVAL '1 day')::date
         WHERE page_id = %s
           AND assigned_to IS NULL
        """,
        (effective_date, page_id),
    )
    cur.execute(
        """
        INSERT INTO fb_page_shop_binding
            (page_id, pos_shop_id, assigned_from, assigned_by, reason, note)
        VALUES (%s, %s, COALESCE(%s::date, CURRENT_DATE), %s, %s, %s)
        RETURNING id
        """,
        (page_id, pos_shop_id, effective_date, assigned_by, reason, note),
    )
    return int(cur.fetchone()[0])


def get_active_page_binding(cur, page_id: str) -> Optional[Dict[str, Any]]:
    """Lấy binding active của 1 page."""
    cur.execute(
        """
        SELECT b.id, b.pos_shop_id, s.shop_name, s.shop_key,
               b.assigned_from, b.note, b.reason
          FROM fb_page_shop_binding b
          LEFT JOIN shops s ON s.id = b.pos_shop_id
         WHERE b.page_id = %s AND b.assigned_to IS NULL
         LIMIT 1
        """,
        (page_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "binding_id":    int(row[0]),
        "pos_shop_id":   int(row[1]) if row[1] is not None else None,
        "shop_name":     row[2] or "",
        "shop_key":      row[3] or "",
        "assigned_from": row[4],
        "note":          row[5] or "",
        "reason":        row[6] or "",
    }


def list_page_bindings_active(cur) -> Dict[str, Dict[str, Any]]:
    """Dict page_id → {pos_shop_id, shop_name} của mọi binding active."""
    cur.execute(
        """
        SELECT b.page_id, b.pos_shop_id, s.shop_name, s.shop_key
          FROM fb_page_shop_binding b
          LEFT JOIN shops s ON s.id = b.pos_shop_id
         WHERE b.assigned_to IS NULL
        """
    )
    result: Dict[str, Dict[str, Any]] = {}
    for r in cur.fetchall():
        result[str(r[0])] = {
            "pos_shop_id": int(r[1]) if r[1] is not None else None,
            "shop_name":   r[2] or "",
            "shop_key":    r[3] or "",
        }
    return result


def list_pages_with_spend_range(
    cur,
    date_from: str,
    date_to: str,
    only_unbound: bool = False,
) -> List[Dict[str, Any]]:
    """Liệt kê page có spend trong range [date_from, date_to] + binding hiện tại.

    Giống `list_pages_with_recent_spend` nhưng filter theo range cụ thể.
    date_from / date_to ở định dạng 'YYYY-MM-DD' (inclusive).
    """
    cur.execute(
        """
        WITH recent AS (
            SELECT s.page_id,
                   MAX(s.page_name)        AS page_name,
                   SUM(s.spend)            AS total_spend,
                   MAX(s.metric_date)      AS last_seen
              FROM fb_ads_page_daily_spend s
             WHERE s.metric_date >= %s AND s.metric_date <= %s
             GROUP BY s.page_id
        ),
        recent_accounts AS (
            SELECT s.page_id,
                   s.fb_ad_account_id,
                   COALESCE(ai.account_name, s.fb_ad_account_id) AS account_name,
                   SUM(s.spend) AS account_spend
              FROM fb_ads_page_daily_spend s
              LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
             WHERE s.metric_date >= %s AND s.metric_date <= %s
             GROUP BY s.page_id, s.fb_ad_account_id, ai.account_name
        ),
        accounts_agg AS (
            -- Order theo spend DESC để phần tử đầu = TK QC chính (primary)
            SELECT page_id,
                   string_agg(fb_ad_account_id, '|' ORDER BY account_spend DESC) AS account_ids,
                   string_agg(account_name, '|' ORDER BY account_spend DESC) AS account_names
              FROM recent_accounts
             GROUP BY page_id
        )
        SELECT r.page_id, r.page_name, r.total_spend, r.last_seen,
               a.account_ids, a.account_names,
               b.pos_shop_id, sh.shop_name, sh.shop_key,
               b.assigned_from, b.note,
               (pp.page_id IS NULL) AS not_in_pa_pages
          FROM recent r
          LEFT JOIN accounts_agg a ON a.page_id = r.page_id
          LEFT JOIN fb_page_shop_binding b
                 ON b.page_id = r.page_id AND b.assigned_to IS NULL
          LEFT JOIN shops sh ON sh.id = b.pos_shop_id
          LEFT JOIN pa_pages pp ON pp.page_id = r.page_id
         ORDER BY r.total_spend DESC
        """,
        (date_from, date_to, date_from, date_to),
    )
    rows = []
    for r in cur.fetchall():
        page_name = (r[1] or "").strip()
        not_in_pa = bool(r[11])
        account_ids_raw = r[4] or ""
        account_names_raw = r[5] or ""
        ids = [x.strip() for x in account_ids_raw.split("|") if x.strip()]
        names = [x.strip() for x in account_names_raw.split("|") if x.strip()]
        accounts: List[Dict[str, str]] = []
        for i, aid in enumerate(ids):
            nm = names[i] if i < len(names) else aid
            accounts.append({"id": aid, "name": nm or aid})
        is_zombie = (
            not page_name
            or (page_name.lower().startswith("page ") and page_name[5:].strip().isdigit())
            or not_in_pa
        )
        binding_shop = r[7] or ""
        has_binding_row = r[6] is not None or r[9] is not None
        rows.append({
            "page_id":            str(r[0]),
            "page_name":          page_name or f"Page {r[0]}",
            "total_spend":        float(r[2] or 0),
            "last_seen":          r[3],
            "fb_ad_account_ids":  account_ids_raw.replace("|", ", "),
            "accounts":           accounts,
            "binding_pos_shop_id": int(r[6]) if r[6] is not None else None,
            "binding_shop_name":  binding_shop,
            "binding_shop_key":   r[8] or "",
            "assigned_from":      r[9],
            "note":               r[10] or "",
            "is_zombie":          is_zombie,
            "is_reviewed_test":   (r[6] is None and r[9] is not None),
            "has_binding_row":    has_binding_row,
        })
    if only_unbound:
        rows = [x for x in rows if not x["has_binding_row"]]
    return rows


def list_pages_with_recent_spend(
    cur,
    days: int = 7,
    only_unbound: bool = False,
) -> List[Dict[str, Any]]:
    """Liệt kê page có spend trong N ngày gần đây + binding hiện tại.

    Trả về [{page_id, page_name, total_spend, last_seen, fb_ad_account_ids,
             binding_pos_shop_id, binding_shop_name, is_zombie}, ...]
    is_zombie = page_name rỗng / pattern "Page <số>" / không có row trong pa_pages.
    """
    cur.execute(
        f"""
        WITH recent AS (
            SELECT s.page_id,
                   MAX(s.page_name)        AS page_name,
                   SUM(s.spend)            AS total_spend,
                   MAX(s.metric_date)      AS last_seen
              FROM fb_ads_page_daily_spend s
             WHERE s.metric_date >= (CURRENT_DATE - INTERVAL '{int(days)} days')
             GROUP BY s.page_id
        ),
        recent_accounts AS (
            SELECT s.page_id,
                   s.fb_ad_account_id,
                   COALESCE(ai.account_name, s.fb_ad_account_id) AS account_name
              FROM fb_ads_page_daily_spend s
              LEFT JOIN fb_ad_account_info ai ON ai.ad_account_id = s.fb_ad_account_id
             WHERE s.metric_date >= (CURRENT_DATE - INTERVAL '{int(days)} days')
             GROUP BY s.page_id, s.fb_ad_account_id, ai.account_name
        ),
        accounts_agg AS (
            SELECT page_id,
                   string_agg(DISTINCT fb_ad_account_id, '|' ORDER BY fb_ad_account_id) AS account_ids,
                   string_agg(DISTINCT account_name, '|' ORDER BY account_name) AS account_names
              FROM recent_accounts
             GROUP BY page_id
        )
        SELECT r.page_id, r.page_name, r.total_spend, r.last_seen,
               a.account_ids, a.account_names,
               b.pos_shop_id, sh.shop_name, sh.shop_key,
               b.assigned_from, b.note,
               (pp.page_id IS NULL) AS not_in_pa_pages
          FROM recent r
          LEFT JOIN accounts_agg a ON a.page_id = r.page_id
          LEFT JOIN fb_page_shop_binding b
                 ON b.page_id = r.page_id AND b.assigned_to IS NULL
          LEFT JOIN shops sh ON sh.id = b.pos_shop_id
          LEFT JOIN pa_pages pp ON pp.page_id = r.page_id
         ORDER BY r.total_spend DESC
        """
    )
    rows = []
    for r in cur.fetchall():
        page_name = (r[1] or "").strip()
        not_in_pa = bool(r[11])
        account_ids_raw = r[4] or ""
        account_names_raw = r[5] or ""
        # Build list of (id, name) tuples for template
        ids = [x.strip() for x in account_ids_raw.split("|") if x.strip()]
        names = [x.strip() for x in account_names_raw.split("|") if x.strip()]
        # Pair them — names may be fewer if some accounts share account_name → fallback to id
        accounts: List[Dict[str, str]] = []
        for i, aid in enumerate(ids):
            nm = names[i] if i < len(names) else aid
            accounts.append({"id": aid, "name": nm or aid})
        # Zombie: tên rỗng, hoặc pattern "Page <digits>", hoặc không có trong pa_pages
        is_zombie = (
            not page_name
            or (page_name.lower().startswith("page ") and page_name[5:].strip().isdigit())
            or not_in_pa
        )
        binding_shop = r[7] or ""
        # Nếu có binding row với pos_shop_id NULL → đã review, là test page
        has_binding_row = r[6] is not None or r[9] is not None
        rows.append({
            "page_id":            str(r[0]),
            "page_name":          page_name or f"Page {r[0]}",
            "total_spend":        float(r[2] or 0),
            "last_seen":          r[3],
            "fb_ad_account_ids":  account_ids_raw.replace("|", ", "),
            "accounts":           accounts,
            "binding_pos_shop_id": int(r[6]) if r[6] is not None else None,
            "binding_shop_name":  binding_shop,
            "binding_shop_key":   r[8] or "",
            "assigned_from":      r[9],
            "note":               r[10] or "",
            "is_zombie":          is_zombie,
            "is_reviewed_test":   (r[6] is None and r[9] is not None),
            "has_binding_row":    has_binding_row,
        })
    if only_unbound:
        rows = [x for x in rows if not x["has_binding_row"]]
    return rows


def list_page_binding_history(cur, page_id: str) -> List[Dict[str, Any]]:
    """Lịch sử binding của 1 page."""
    cur.execute(
        """
        SELECT b.id, b.pos_shop_id, s.shop_name, s.shop_key,
               b.assigned_from, b.assigned_to,
               u.username AS by_username, b.reason, b.note, b.created_at
          FROM fb_page_shop_binding b
          LEFT JOIN shops s ON s.id = b.pos_shop_id
          LEFT JOIN users u ON u.id = b.assigned_by
         WHERE b.page_id = %s
         ORDER BY b.assigned_from DESC, b.id DESC
        """,
        (page_id,),
    )
    return [
        {
            "id": int(r[0]),
            "pos_shop_id":   int(r[1]) if r[1] is not None else None,
            "shop_name":     r[2] or "",
            "shop_key":      r[3] or "",
            "assigned_from": r[4],
            "assigned_to":   r[5],
            "assigned_by_username": r[6] or "",
            "reason":        r[7] or "",
            "note":          r[8] or "",
            "created_at":    r[9],
        }
        for r in cur.fetchall()
    ]


def bootstrap_summary_counts(cur) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for table_name in ["teams", "users", "shops", "user_shop_assignments", "webs"]:
        cur.execute(f"SELECT COUNT(1) FROM {table_name}")
        row = cur.fetchone()
        result[table_name] = int(row[0]) if row else 0
    return result
