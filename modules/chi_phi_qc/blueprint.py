"""Blueprint + auth guard cho module Chi phí QC.

Tách riêng để routes.py và __init__.py cùng import được `chi_phi_qc_bp`
mà không tạo vòng import.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, redirect, session

from modules.chi_phi_qc.helpers import _load_users_json

chi_phi_qc_bp = Blueprint(
    "chi_phi_qc", __name__,
    template_folder="templates",
    url_prefix="/chi-phi-qc",
)

# ── Auth guard ───────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        username = session.get("username")
        if not session.get("user_id") and not username:
            return redirect(f"/login?next=/chi-phi-qc/")
        # Backfill user_id into session if it's missing (legacy sessions)
        if not session.get("user_id") and username:
            for u in _load_users_json():
                if str(u.get("username", "")).strip() == username.strip():
                    session["user_id"] = u.get("id")
                    session["full_name"] = u.get("full_name", "") or username
                    break
        return f(*args, **kwargs)
    return decorated
