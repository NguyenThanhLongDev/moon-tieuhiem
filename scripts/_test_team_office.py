#!/usr/bin/env python3
"""Test nhanh: tạo user tạm gán team → kiểm tra tự nhận văn phòng chấm công → XOÁ SẠCH.

Chạy: DATABASE_URL=... .venv/bin/python3 scripts/_test_team_office.py [team_code]
Mặc định team_code = team-cong-minh. Không để lại dữ liệu rác (tự cleanup cuối).
"""
import sys, os
_BASE = os.path.join(os.path.dirname(__file__), '..')
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from db import get_conn
from modules.cham_cong.cc_db import get_office_for_team, apply_team_office_to_user

TEAM = (sys.argv[1] if len(sys.argv) > 1 else "team-cong-minh").strip()
TEST_USERNAME = "__test_office_tmp__"

def main():
    office_id = get_office_for_team(TEAM)
    print(f"[1] Văn phòng mặc định của team '{TEAM}': office_id={office_id}")
    if office_id is None:
        print(f"   ⚠ Team '{TEAM}' CHƯA cấu hình văn phòng mặc định (cc_team_office trống cho team này).")
        print(f"   → Vào Chấm công→Cài đặt bấm 'Gán cho team' cho team này trước, rồi test lại.")
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, team_name FROM teams WHERE team_code=%s", (TEAM,))
            r = cur.fetchone()
            if not r:
                print(f"   ✗ Không tìm thấy team_code '{TEAM}' trong bảng teams."); return
            team_id, team_name = r
            office_name = None
            cur.execute("SELECT name FROM cc_offices WHERE id=%s", (office_id,))
            o = cur.fetchone()
            office_name = o[0] if o else "?"
            print(f"    Team '{team_name}' (id={team_id}) → văn phòng '{office_name}'")

            # Tạo user tạm
            cur.execute("""
                INSERT INTO users (username, password_hash, full_name, role, team_id, status)
                VALUES (%s, 'x', 'TEST Office', 'staff'::user_role, %s, 'active'::record_status)
                ON CONFLICT (username) DO UPDATE SET team_id=EXCLUDED.team_id
                RETURNING id
            """, (TEST_USERNAME, team_id))
            uid = cur.fetchone()[0]
            conn.commit()
            print(f"[2] Tạo user tạm id={uid} (username={TEST_USERNAME}) gán team '{TEAM}'")

    # Gọi đúng hàm production
    ok = apply_team_office_to_user(uid, TEAM, "TEST Office")
    print(f"[3] apply_team_office_to_user → {ok}")

    # Kiểm tra cc_employees.office_id
    passed = False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT office_id FROM cc_employees WHERE user_id=%s", (str(uid),))
            r = cur.fetchone()
            got = r[0] if r else None
            print(f"[4] cc_employees.office_id của user tạm = {got} (mong đợi {office_id})")
            passed = (got == office_id)

            # CLEANUP — xoá sạch
            cur.execute("DELETE FROM cc_employees WHERE user_id=%s", (str(uid),))
            cur.execute("DELETE FROM users WHERE id=%s AND username=%s", (uid, TEST_USERNAME))
            conn.commit()
            print(f"[5] Đã xoá sạch user tạm + cc_employees (cleanup OK)")

    print("\n==> KẾT QUẢ:", "✅ PASS — NV mới tự nhận đúng văn phòng theo team" if passed
          else "❌ FAIL — office không khớp, cần xem lại")

if __name__ == "__main__":
    main()
