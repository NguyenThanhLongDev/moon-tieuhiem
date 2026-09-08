#!/usr/bin/env python3
"""
One-time migration: users.json → PostgreSQL
- Thêm 'accountant' vào user_role enum
- Tạo teams từ team_id trong users.json
- Import users với bcrypt hash
- Import user_shop_assignments
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))

import bcrypt
import psycopg2

DB_URL = os.getenv("DATABASE_URL", "").strip()
if not DB_URL:
    # fallback: đọc từ deploy env file
    env_path = os.path.join(os.path.dirname(__file__), "deploy", "pos-dashboard.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                DB_URL = line[len("DATABASE_URL="):]
                break

if not DB_URL:
    print("ERROR: Không tìm thấy DATABASE_URL")
    sys.exit(1)

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")

def load_users_json():
    with open(USERS_FILE) as f:
        return json.load(f)

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()

def run():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # 1. Thêm 'accountant' vào user_role enum nếu chưa có
    cur.execute("SELECT unnest(enum_range(NULL::user_role))::text")
    existing_roles = {r[0] for r in cur.fetchall()}
    if "accountant" not in existing_roles:
        print("→ Thêm 'accountant' vào user_role enum...")
        cur.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'accountant'")
        conn.commit()
        print("   Done.")
    else:
        print("✓ 'accountant' đã có trong enum")

    users = load_users_json()

    # 2. Tạo teams
    TEAM_NAMES = {
        "team-nam":   "Team Nam",
        "team-minh":  "Team Minh",
        "team-thanh": "Team Thanh",
        "team-ken":   "Team Ken",
        "team-nhat":  "Team Nhat",
    }
    print("\n→ Tạo/upsert teams...")
    team_code_to_id = {}
    for code, name in TEAM_NAMES.items():
        cur.execute("""
            INSERT INTO teams (team_code, team_name, status)
            VALUES (%s, %s, 'active')
            ON CONFLICT (team_code) DO UPDATE SET team_name = EXCLUDED.team_name
            RETURNING id
        """, (code, name))
        row = cur.fetchone()
        team_code_to_id[code] = row[0]
        print(f"   {code} → id={row[0]}")
    conn.commit()

    # Lấy shop_key → shop_id mapping
    cur.execute("SELECT shop_key, id FROM shops")
    shop_key_to_id = {r[0]: r[1] for r in cur.fetchall()}

    # 3. Import users
    print("\n→ Import users...")
    username_to_db_id = {}
    skipped = []
    inserted = []

    for u in users:
        username   = u.get("username", "").strip()
        plain_pw   = str(u.get("password", "1221"))
        role       = u.get("role", "staff")
        team_code  = str(u.get("team_id", "")).strip()
        status     = u.get("status", "active")
        full_name  = u.get("full_name") or username

        # Map role: các role lạ → staff
        valid_roles = {"admin", "leader", "staff", "accountant"}
        if role not in valid_roles:
            role = "staff"

        team_db_id = team_code_to_id.get(team_code) if team_code else None
        pw_hash    = hash_password(plain_pw)

        try:
            cur.execute("""
                INSERT INTO users (username, password_hash, full_name, role, team_id, status)
                VALUES (%s, %s, %s, %s::user_role, %s, %s::record_status)
                ON CONFLICT (username) DO UPDATE SET
                    password_hash = EXCLUDED.password_hash,
                    full_name     = EXCLUDED.full_name,
                    role          = EXCLUDED.role,
                    team_id       = EXCLUDED.team_id,
                    status        = EXCLUDED.status
                RETURNING id
            """, (username, pw_hash, full_name, role, team_db_id, status))
            db_id = cur.fetchone()[0]
            username_to_db_id[username] = db_id
            inserted.append(username)
        except Exception as e:
            skipped.append((username, str(e)))

    conn.commit()
    print(f"   Inserted/updated: {len(inserted)} users")
    if skipped:
        print(f"   Skipped: {skipped}")

    # 4. Import user_shop_assignments
    print("\n→ Import user_shop_assignments...")
    assigned_count = 0
    for u in users:
        username = u.get("username", "").strip()
        assigned = u.get("assigned_shops", [])
        user_db_id = username_to_db_id.get(username)
        if not user_db_id:
            continue
        if "*" in assigned:
            continue  # admin/accountant: skip, quyền được handle ở code

        for shop_key in assigned:
            shop_db_id = shop_key_to_id.get(shop_key)
            if not shop_db_id:
                continue
            try:
                cur.execute("""
                    INSERT INTO user_shop_assignments (user_id, shop_id)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id, shop_id) DO NOTHING
                """, (user_db_id, shop_db_id))
                assigned_count += 1
            except Exception as e:
                print(f"   WARN shop assignment {username}/{shop_key}: {e}")

    conn.commit()
    print(f"   Assigned: {assigned_count} shop assignments")

    # 5. Kiểm tra kết quả
    cur.execute("SELECT count(*) FROM users")
    print(f"\n✓ Tổng users trong DB: {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM teams")
    print(f"✓ Tổng teams trong DB: {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM user_shop_assignments")
    print(f"✓ Tổng user_shop_assignments: {cur.fetchone()[0]}")

    cur.close()
    conn.close()
    print("\n✅ Migration hoàn tất!")

if __name__ == "__main__":
    run()
