"""DB helpers cho module Page & Tài khoản."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


MIGRATIONS = [
    # 001 — bảng lưu FB token của IT
    """
    CREATE TABLE IF NOT EXISTS pa_fb_tokens (
        id              SERIAL PRIMARY KEY,
        fb_user_id      TEXT NOT NULL UNIQUE,
        fb_user_name    TEXT,
        access_token    TEXT NOT NULL,
        added_by        TEXT,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    # 002 — Business Managers
    """
    CREATE TABLE IF NOT EXISTS pa_business_managers (
        bm_id           TEXT PRIMARY KEY,
        bm_name         TEXT NOT NULL,
        permitted_roles TEXT[],
        fb_user_id      TEXT,
        synced_at       TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    # 003 — Pages
    """
    CREATE TABLE IF NOT EXISTS pa_pages (
        page_id         TEXT PRIMARY KEY,
        page_name       TEXT NOT NULL,
        category        TEXT,
        picture_url     TEXT,
        username        TEXT,
        is_published    BOOLEAN DEFAULT TRUE,
        tasks           TEXT[],
        bm_id           TEXT REFERENCES pa_business_managers(bm_id) ON DELETE SET NULL,
        bm_relation     TEXT DEFAULT 'owned',
        synced_at       TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    # 004 — Ad Accounts
    """
    CREATE TABLE IF NOT EXISTS pa_ad_accounts (
        account_id      TEXT PRIMARY KEY,
        account_name    TEXT NOT NULL,
        account_status  INT DEFAULT 1,
        currency        TEXT,
        timezone_name   TEXT,
        bm_id           TEXT REFERENCES pa_business_managers(bm_id) ON DELETE SET NULL,
        bm_relation     TEXT DEFAULT 'owned',
        synced_at       TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    # 005 — token_type column (nếu DB cũ chưa có)
    """
    ALTER TABLE pa_fb_tokens ADD COLUMN IF NOT EXISTS token_type TEXT DEFAULT 'user'
    """,
    # 006 — expires_at để hiển thị ngày hết hạn chính xác
    """
    ALTER TABLE pa_fb_tokens ADD COLUMN IF NOT EXISTS expires_at BIGINT DEFAULT 0
    """,
    # 007 — data_access_expires_at (FB cấp 90 ngày song song)
    """
    ALTER TABLE pa_fb_tokens ADD COLUMN IF NOT EXISTS data_access_expires_at BIGINT DEFAULT 0
    """,
]


def run_migrations(conn) -> None:
    with conn.cursor() as cur:
        for sql in MIGRATIONS:
            cur.execute(sql)
    conn.commit()


# ── Token ─────────────────────────────────────────────────────────────

def upsert_token(conn, fb_user_id: str, fb_user_name: str,
                 access_token: str, added_by: str,
                 token_type: str = "user",
                 expires_at: int = 0,
                 data_access_expires_at: int = 0) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pa_fb_tokens (fb_user_id, fb_user_name, access_token, added_by, token_type, expires_at, data_access_expires_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (fb_user_id) DO UPDATE
            SET fb_user_name=EXCLUDED.fb_user_name,
                access_token=EXCLUDED.access_token,
                added_by=EXCLUDED.added_by,
                token_type=EXCLUDED.token_type,
                expires_at=EXCLUDED.expires_at,
                data_access_expires_at=EXCLUDED.data_access_expires_at,
                updated_at=NOW()
        """, (fb_user_id, fb_user_name, access_token, added_by, token_type,
              expires_at, data_access_expires_at))
    conn.commit()


def get_all_tokens(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fb_user_id, fb_user_name, added_by, updated_at, token_type,
                   COALESCE(expires_at, 0) AS expires_at,
                   COALESCE(data_access_expires_at, 0) AS data_access_expires_at
            FROM pa_fb_tokens ORDER BY updated_at DESC
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_token(conn, fb_user_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT access_token FROM pa_fb_tokens WHERE fb_user_id=%s", (fb_user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def delete_token(conn, fb_user_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pa_fb_tokens WHERE fb_user_id=%s", (fb_user_id,))
    conn.commit()


# ── Sync BM + Pages + Ad Accounts ─────────────────────────────────────

def upsert_bm(conn, bm_id: str, bm_name: str, roles: list[str], fb_user_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pa_business_managers (bm_id, bm_name, permitted_roles, fb_user_id, synced_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (bm_id) DO UPDATE
            SET bm_name=EXCLUDED.bm_name,
                permitted_roles=EXCLUDED.permitted_roles,
                fb_user_id=EXCLUDED.fb_user_id,
                synced_at=NOW()
        """, (bm_id, bm_name, roles, fb_user_id))
    conn.commit()


def upsert_page(conn, page: dict, bm_id: str) -> None:
    pic = (page.get("picture") or {}).get("data", {}).get("url") or None
    tasks = page.get("tasks") or []
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pa_pages
              (page_id, page_name, category, picture_url, username,
               is_published, tasks, bm_id, bm_relation, synced_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (page_id) DO UPDATE
            SET page_name=EXCLUDED.page_name, category=EXCLUDED.category,
                picture_url=EXCLUDED.picture_url, username=EXCLUDED.username,
                is_published=EXCLUDED.is_published, tasks=EXCLUDED.tasks,
                bm_id=EXCLUDED.bm_id, bm_relation=EXCLUDED.bm_relation, synced_at=NOW()
        """, (
            str(page["id"]), page.get("name",""), page.get("category",""),
            pic, page.get("username",""), page.get("is_published", True),
            tasks, bm_id, page.get("_relation","owned"),
        ))
    conn.commit()


def upsert_ad_account(conn, acc: dict, bm_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pa_ad_accounts
              (account_id, account_name, account_status, currency,
               timezone_name, bm_id, bm_relation, synced_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (account_id) DO UPDATE
            SET account_name=EXCLUDED.account_name,
                account_status=EXCLUDED.account_status,
                currency=EXCLUDED.currency, timezone_name=EXCLUDED.timezone_name,
                bm_id=EXCLUDED.bm_id, bm_relation=EXCLUDED.bm_relation, synced_at=NOW()
        """, (
            str(acc["id"]), acc.get("name",""),
            int(acc.get("account_status") or 1),
            acc.get("currency",""), acc.get("timezone_name",""),
            bm_id, acc.get("_relation","owned"),
        ))
    conn.commit()


# ── Queries ───────────────────────────────────────────────────────────

def get_all_bms(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.bm_id, b.bm_name, b.permitted_roles, b.fb_user_id, b.synced_at,
                   COUNT(DISTINCT p.page_id) AS page_count,
                   COUNT(DISTINCT a.account_id) AS account_count
            FROM pa_business_managers b
            LEFT JOIN pa_pages p ON p.bm_id = b.bm_id
            LEFT JOIN pa_ad_accounts a ON a.bm_id = b.bm_id
            GROUP BY b.bm_id ORDER BY b.bm_name
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_pages(conn, bm_id: str | None = None) -> list[dict]:
    with conn.cursor() as cur:
        if bm_id:
            cur.execute("""
                SELECT p.*, b.bm_name FROM pa_pages p
                LEFT JOIN pa_business_managers b ON b.bm_id = p.bm_id
                WHERE p.bm_id=%s ORDER BY p.page_name
            """, (bm_id,))
        else:
            cur.execute("""
                SELECT p.*, b.bm_name FROM pa_pages p
                LEFT JOIN pa_business_managers b ON b.bm_id = p.bm_id
                ORDER BY b.bm_name NULLS LAST, p.page_name
            """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_ad_accounts(conn, bm_id: str | None = None) -> list[dict]:
    # TK bị loại tay (không phải công ty dù chung token/BM) — không hiện ở đây.
    with conn.cursor() as cur:
        if bm_id:
            cur.execute("""
                SELECT a.*, b.bm_name FROM pa_ad_accounts a
                LEFT JOIN pa_business_managers b ON b.bm_id = a.bm_id
                WHERE a.bm_id=%s
                  AND NOT EXISTS (SELECT 1 FROM fb_ad_account_exclude e
                                   WHERE e.ad_account_id = REPLACE(a.account_id,'act_',''))
                ORDER BY a.account_name
            """, (bm_id,))
        else:
            cur.execute("""
                SELECT a.*, b.bm_name FROM pa_ad_accounts a
                LEFT JOIN pa_business_managers b ON b.bm_id = a.bm_id
                WHERE NOT EXISTS (SELECT 1 FROM fb_ad_account_exclude e
                                   WHERE e.ad_account_id = REPLACE(a.account_id,'act_',''))
                ORDER BY b.bm_name NULLS LAST, a.account_name
            """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
