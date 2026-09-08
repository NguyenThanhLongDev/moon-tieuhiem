from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from db import get_conn  # noqa: E402
from repositories.admin_repo import (  # noqa: E402
    bootstrap_summary_counts,
    get_team_id_by_code,
    upsert_shop,
    upsert_team,
    upsert_user,
    upsert_user_shop_assignment,
    upsert_web,
)

USERS_FILE = BASE_DIR / "users.json"
SHOPS_FILE = BASE_DIR / "shops.json"
WEBSITES_FILE = BASE_DIR / "websites.json"


def read_json(path: Path) -> Any:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def norm_status(value: Any) -> str:
    status = str(value or "active").strip().lower()
    return "inactive" if status == "inactive" else "active"


def norm_role(value: Any) -> str:
    role = str(value or "staff").strip().lower()
    if role in {"admin", "leader", "staff"}:
        return role
    return "staff"


def password_to_hash(raw_password: Any) -> str:
    text = str(raw_password or "").strip()
    if not text:
        text = "changeme"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_domain(url: str) -> Optional[str]:
    text = str(url or "").strip()
    if not text:
        return None
    text = text.replace("https://", "").replace("http://", "")
    return text.split("/")[0].strip() or None


def bootstrap_core_admin() -> None:
    users = read_json(USERS_FILE)
    shops = read_json(SHOPS_FILE)
    websites = read_json(WEBSITES_FILE)

    if not isinstance(users, list):
        users = []
    if not isinstance(shops, list):
        shops = []
    if not isinstance(websites, list):
        websites = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            team_code_to_id: Dict[str, int] = {}

            # 1) Build teams from users + shops team_id
            discovered_team_codes = set()
            for u in users:
                if isinstance(u, dict):
                    team_code = str(u.get("team_id", "")).strip()
                    if team_code:
                        discovered_team_codes.add(team_code)
            for s in shops:
                if isinstance(s, dict):
                    team_code = str(s.get("team_id", "")).strip()
                    if team_code:
                        discovered_team_codes.add(team_code)

            for team_code in sorted(discovered_team_codes):
                team_name = team_code
                team_id = upsert_team(cur, team_code=team_code, team_name=team_name, status="active")
                team_code_to_id[team_code] = team_id

            # 2) Upsert users
            username_to_user_id: Dict[str, int] = {}
            for u in users:
                if not isinstance(u, dict):
                    continue
                username = str(u.get("username", "")).strip()
                if not username:
                    continue
                role = norm_role(u.get("role"))
                status = norm_status(u.get("status"))
                team_code = str(u.get("team_id", "")).strip()
                team_id = team_code_to_id.get(team_code)
                user_id = upsert_user(
                    cur,
                    username=username,
                    password_hash=password_to_hash(u.get("password")),
                    role=role,
                    team_id=team_id,
                    status=status,
                )
                username_to_user_id[username] = user_id

            # 3) Upsert shops
            shop_key_to_shop_id: Dict[str, int] = {}
            for s in shops:
                if not isinstance(s, dict):
                    continue
                shop_key = str(s.get("shop_key", "")).strip()
                if not shop_key:
                    continue
                shop_name = str(s.get("shop_name", "")).strip() or shop_key
                shop_code = str(s.get("shop_id", "")).strip() or None
                status = norm_status(s.get("status"))
                team_code = str(s.get("team_id", "")).strip()
                team_id = team_code_to_id.get(team_code)
                shop_id = upsert_shop(
                    cur,
                    shop_key=shop_key,
                    shop_name=shop_name,
                    shop_code=shop_code,
                    team_id=team_id,
                    status=status,
                )
                shop_key_to_shop_id[shop_key] = shop_id

            # 4) Assign leader_user_id on teams
            for u in users:
                if not isinstance(u, dict):
                    continue
                if norm_role(u.get("role")) != "leader":
                    continue
                username = str(u.get("username", "")).strip()
                team_code = str(u.get("team_id", "")).strip()
                if not username or not team_code:
                    continue
                user_id = username_to_user_id.get(username)
                team_id = team_code_to_id.get(team_code) or get_team_id_by_code(cur, team_code)
                if not user_id or not team_id:
                    continue
                cur.execute(
                    "UPDATE teams SET leader_user_id = %s WHERE id = %s",
                    (user_id, team_id),
                )

            # 5) Upsert user-shop assignments from users.assigned_shops
            for u in users:
                if not isinstance(u, dict):
                    continue
                username = str(u.get("username", "")).strip()
                user_id = username_to_user_id.get(username)
                if not user_id:
                    continue
                assigned_shops = u.get("assigned_shops", [])
                if isinstance(assigned_shops, str):
                    assigned_shops = [x.strip() for x in assigned_shops.split(",") if x.strip()]
                if not isinstance(assigned_shops, list):
                    assigned_shops = []
                for shop_key in assigned_shops:
                    key = str(shop_key).strip()
                    if not key or key == "*":
                        continue
                    shop_id = shop_key_to_shop_id.get(key)
                    if not shop_id:
                        continue
                    upsert_user_shop_assignment(cur, user_id=user_id, shop_id=shop_id, assigned_by=None)

            # 6) Upsert webs from websites.json
            for w in websites:
                if not isinstance(w, dict):
                    continue
                shop_key = str(w.get("shop_key", "")).strip()
                shop_id = shop_key_to_shop_id.get(shop_key)
                if not shop_id:
                    continue
                web_name = str(w.get("name", "")).strip() or "web"
                domain = extract_domain(str(w.get("url", "")).strip())
                status = norm_status(w.get("status"))
                upsert_web(cur, shop_id=shop_id, web_name=web_name, domain=domain, status=status)

            counts = bootstrap_summary_counts(cur)
            print("Bootstrap completed.")
            print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is required. Example: export DATABASE_URL=postgresql://user:pass@host:5432/db")
    bootstrap_core_admin()
