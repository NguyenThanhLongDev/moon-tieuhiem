"""Tính toán/biến đổi dữ liệu cho trang Chi phí QC.

Tách từ __init__.py — nhận dữ liệu thô (từ db_queries) và dựng thành
cấu trúc mà template cần. Phụ thuộc helpers.py + db_queries.py.
"""
from __future__ import annotations

from modules.chi_phi_qc.db_queries import (
    _page_account_spend_in_range,
    _shop_keys_to_account_ids,
)
from modules.chi_phi_qc.helpers import _get_team_members, _normalize_shops


def _build_team_groups(leader_user_id, leader_team_id: str) -> list:
    """
    For leader/admin view: return list of {user, shop_keys, account_ids}
    so the template can group pages by employee.
    """
    members = _get_team_members(leader_team_id) if leader_team_id else []
    groups = []
    for m in members:
        shops = _normalize_shops(m.get("assigned_shops", []))
        acc_ids = _shop_keys_to_account_ids(shops)
        groups.append({
            "user_id":   m.get("id"),
            "username":  m.get("username", ""),
            "full_name": m.get("full_name", "") or m.get("username", ""),
            "role":      m.get("role", "staff"),
            "team_id":   m.get("team_id", ""),
            "shop_keys": shops,
            "account_ids": acc_ids,
        })
    return groups

# ── Tách page theo NV phụ trách trong kỳ ─────────────────────────────
def _split_page_entry_by_owner(entry: dict, owner_segments: dict,
                               date_from: str, date_to: str) -> list:
    """Nếu TK của page entry có nhiều NV phụ trách trong khoảng → tách thành N entries.
    Mỗi entry: dùng owner riêng + spend cộng dồn trong segment đó.
    Trả về list 1 phần tử (giữ nguyên) hoặc N phần tử (đã tách).
    """
    accs = entry.get("ad_accounts") or []
    if not accs:
        return [entry]
    aid = (accs[0].get("ad_account_id") or "")
    segs = owner_segments.get(aid) or []
    if len(segs) <= 1:
        return [entry]
    # Tách: mỗi segment 1 entry
    result = []
    seg_total_spend = 0.0   # cộng dồn spend đã gán segment → để tính phần GAP còn lại
    seg_total_impr  = 0
    seg_total_click = 0
    for seg in segs:
        sub_spend = _page_account_spend_in_range(
            entry["page_id"], aid, seg["seg_from"], seg["seg_to"]
        )
        seg_total_spend += float(sub_spend["spend"] or 0)
        seg_total_impr  += int(sub_spend["impressions"] or 0)
        seg_total_click += int(sub_spend["clicks"] or 0)
        new_acc = {
            **accs[0],
            "ad_account_name": seg.get("name_snapshot") or accs[0].get("ad_account_name") or aid,
            "owner_id":       seg["owner_id"],
            "owner_name":     seg["owner_name"],
            "owner_username": seg["owner_username"],
            "segment_from":   seg["seg_from"].isoformat() if hasattr(seg["seg_from"],"isoformat") else str(seg["seg_from"]),
            "segment_to":     seg["seg_to"].isoformat() if hasattr(seg["seg_to"],"isoformat") else str(seg["seg_to"]),
        }
        new_entry = {**entry}
        new_entry["ad_accounts"] = [new_acc] + accs[1:]  # giữ TK khác nếu có
        new_entry["kpi"] = {
            **(entry.get("kpi") or {}),
            "cp_ads_fb":     sub_spend["spend"],
            "cp_ads_fb_vat": round(sub_spend["spend"] * 1.113, 0),
            "impressions":   sub_spend["impressions"],
            "clicks":        sub_spend["clicks"],
        }
        new_entry["owner_segment"] = {
            "from": new_acc["segment_from"],
            "to":   new_acc["segment_to"],
            "owner_name": seg["owner_name"],
        }
        result.append(new_entry)

    # ── GAP: ngày TK chạy ads nhưng CHƯA gán NV nào (nằm NGOÀI mọi segment) ──
    # Trước đây phần này bị BỎ RƠI khỏi tổng → index thấp hơn báo cáo (~100tr ở kỳ 1-15).
    # Gom vào 1 dòng "⚠ Chưa gán NV" — đối xứng bucket "_unbound" bên báo cáo →
    # tổng index = tổng page-level = báo cáo. (gap = spend cả kỳ − Σ segment)
    full = _page_account_spend_in_range(entry["page_id"], aid, date_from, date_to)
    gap_spend = float(full["spend"] or 0) - seg_total_spend
    if gap_spend > 1.0:
        gap_acc = {
            **accs[0],
            "ad_account_name": accs[0].get("ad_account_name") or aid,
            "owner_id":       None,
            "owner_name":     "⚠ Chưa gán NV",
            "owner_username": "",
            "segment_from":   date_from,
            "segment_to":     date_to,
        }
        gap_entry = {**entry}
        gap_entry["ad_accounts"] = [gap_acc] + accs[1:]
        gap_entry["kpi"] = {
            **(entry.get("kpi") or {}),
            "cp_ads_fb":     gap_spend,
            "cp_ads_fb_vat": round(gap_spend * 1.113, 0),
            "impressions":   max(0, int(full["impressions"] or 0) - seg_total_impr),
            "clicks":        max(0, int(full["clicks"] or 0) - seg_total_click),
        }
        gap_entry["owner_segment"] = {
            "from": date_from, "to": date_to, "owner_name": "⚠ Chưa gán NV",
        }
        result.append(gap_entry)
    return result

# ── Dựng 1 dòng page cho bảng ────────────────────────────────────────
def _build_page_entry(ap, meta, spend_map):
    """Build a unified page dict from an auto-page record."""
    pid = ap["page_id"]
    aid = ap.get("fb_ad_account_id") or ""
    spend = spend_map.get(pid, {
        "win_chua": 0, "win_co": 0,
        "test_chua": 0, "test_co": 0,
        "tong": 0, "count": 0,
    })
    acc_entry = {
        "ad_account_id":   aid,
        "ad_account_name": meta.get("name") or aid,
        "owner_id":        meta.get("owner_id"),
        "owner_name":      meta.get("owner_name"),
        "owner_username":  meta.get("owner_username"),
    } if aid else None
    return {
        "page_id":       pid,
        "page_name":     ap["page_name"],
        "picture_url":   ap.get("picture_url"),
        "page_category": ap.get("page_category"),
        "ad_accounts":   [acc_entry] if acc_entry else [],
        "source":        "auto",
        "spend":         spend,
        "kpi": {
            "cp_ads_fb":     float(ap.get("spend") or 0),
            "cp_ads_fb_vat": round(float(ap.get("spend") or 0) * 1.113, 0),
            "impressions":   int(ap.get("impressions") or 0),
            "clicks":        int(ap.get("clicks") or 0),
            "fb_account_names": meta.get("name") or aid,
        },
    }

