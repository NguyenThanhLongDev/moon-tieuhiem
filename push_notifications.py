"""
Web Push Notifications helper — VAPID-based.

Gửi thông báo đến subscriber đã đăng ký (Chrome/Edge/Firefox/Safari iOS 16.4+).

Usage:
    from push_notifications import send_push_to_user
    send_push_to_user(user_id='longit', title='Công việc mới',
                      body='Admin giao bạn việc XYZ', url='/cham-cong/tasks')

Keys:
    - vapid_private.pem (KHÔNG commit git, đã ở .gitignore)
    - vapid_public.pem  (commit ok, client đọc base64url từ /api/push/vapid-key)
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import serialization
from pywebpush import WebPushException, webpush

log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
VAPID_PRIVATE_PEM = _BASE_DIR / "vapid_private.pem"
VAPID_PUBLIC_PEM = _BASE_DIR / "vapid_public.pem"

# Contact URL cho VAPID claims — Apple yêu cầu URL thật hoặc mailto hợp lệ
# (domain .local bị Apple reject với 403)
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "https://tieuhiem.com")

# Cache public key base64url
_public_key_b64url: Optional[str] = None


def get_vapid_public_key_b64url() -> str:
    """Trả public key định dạng base64url (raw uncompressed point) cho browser."""
    global _public_key_b64url
    if _public_key_b64url is not None:
        return _public_key_b64url
    with open(VAPID_PUBLIC_PEM, "rb") as f:
        pub = serialization.load_pem_public_key(f.read())
    raw = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    _public_key_b64url = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return _public_key_b64url


_private_key_b64url: Optional[str] = None


def _load_private_key_b64url() -> str:
    """pywebpush muốn private scalar (32 bytes) base64url-encoded, không phải PEM."""
    global _private_key_b64url
    if _private_key_b64url is not None:
        return _private_key_b64url
    with open(VAPID_PRIVATE_PEM, "rb") as f:
        priv_key = serialization.load_pem_private_key(f.read(), password=None)
    raw = priv_key.private_numbers().private_value.to_bytes(32, "big")
    _private_key_b64url = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return _private_key_b64url


def send_push_raw(subscription: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    """Gửi 1 thông báo đến 1 subscription. Trả True nếu thành công."""
    sub_info = {
        "endpoint": subscription["endpoint"],
        "keys": {
            "p256dh": subscription["p256dh"],
            "auth": subscription["auth"],
        },
    }
    try:
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=_load_private_key_b64url(),
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=60 * 60 * 24,  # 1 ngày
        )
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        log.warning("push failed status=%s endpoint=%s err=%s",
                    status, subscription["endpoint"][:60], str(e)[:200])
        # Đánh dấu gone nếu 404/410 — caller nên xóa subscription
        if status in (404, 410):
            raise _SubscriptionGone() from e
        return False
    except Exception as e:
        log.exception("push unexpected error: %s", e)
        return False


class _SubscriptionGone(Exception):
    """Subscription bị revoke ở browser — xóa khỏi DB."""


def _get_subscriptions_for_user(user_id: str) -> List[Dict[str, Any]]:
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, endpoint, p256dh, auth "
                "FROM web_push_subscriptions WHERE user_id=%s",
                (str(user_id),),
            )
            rows = cur.fetchall()
    return [
        {"id": r[0], "endpoint": r[1], "p256dh": r[2], "auth": r[3]}
        for r in rows
    ]


def _delete_subscription(sub_id: int) -> None:
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM web_push_subscriptions WHERE id=%s", (sub_id,))
        conn.commit()


def _mark_used(sub_id: int, success: bool) -> None:
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            if success:
                cur.execute(
                    "UPDATE web_push_subscriptions SET last_used_at=NOW(), failure_count=0 WHERE id=%s",
                    (sub_id,),
                )
            else:
                cur.execute(
                    "UPDATE web_push_subscriptions SET failure_count=failure_count+1 WHERE id=%s",
                    (sub_id,),
                )
        conn.commit()


def send_push_to_user(
    user_id: str,
    title: str,
    body: str,
    url: str = "/",
    icon: str = "/static/pwa/icon-192.png",
    tag: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    """
    Gửi push đến TẤT CẢ subscriptions của 1 user (1 user có thể có nhiều device).

    Trả: {'sent': N, 'failed': M, 'removed': K}
    """
    subs = _get_subscriptions_for_user(user_id)
    if not subs:
        log.info("push: user_id=%s không có subscription", user_id)
        return {"sent": 0, "failed": 0, "removed": 0}

    payload = {
        "title": title,
        "body": body,
        "url": url,
        "icon": icon,
        "badge": "/static/pwa/icon-192.png",
        "tag": tag or f"task-{user_id}",
        "extra": extra or {},
    }

    sent = failed = removed = 0
    for sub in subs:
        try:
            ok = send_push_raw(sub, payload)
            if ok:
                sent += 1
                _mark_used(sub["id"], True)
            else:
                failed += 1
                _mark_used(sub["id"], False)
        except _SubscriptionGone:
            _delete_subscription(sub["id"])
            removed += 1
            log.info("push: removed gone subscription id=%s user=%s",
                     sub["id"], user_id)

    log.info("push user=%s sent=%d failed=%d removed=%d",
             user_id, sent, failed, removed)
    return {"sent": sent, "failed": failed, "removed": removed}


def save_subscription(
    user_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: Optional[str] = None,
) -> int:
    """Lưu/update subscription. Trả id."""
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO web_push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (endpoint) DO UPDATE
                    SET user_id=EXCLUDED.user_id,
                        p256dh=EXCLUDED.p256dh,
                        auth=EXCLUDED.auth,
                        user_agent=EXCLUDED.user_agent,
                        failure_count=0
                RETURNING id
                """,
                (str(user_id), endpoint, p256dh, auth, user_agent),
            )
            sub_id = cur.fetchone()[0]
        conn.commit()
    return sub_id


def remove_subscription(endpoint: str) -> bool:
    from db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM web_push_subscriptions WHERE endpoint=%s",
                (endpoint,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted > 0
