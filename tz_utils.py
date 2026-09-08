"""
Timezone utilities — toàn bộ app dùng múi giờ HCM (Asia/Ho_Chi_Minh, UTC+7).
Import: from tz_utils import now_hcm, to_hcm, fmt_hcm, today_hcm
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

HCM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_UTC   = timezone.utc


def now_hcm() -> datetime:
    """Trả về datetime hiện tại theo giờ HCM."""
    return datetime.now(HCM_TZ)


def today_hcm() -> str:
    """Trả về ngày hôm nay theo giờ HCM dạng 'YYYY-MM-DD'."""
    return now_hcm().strftime("%Y-%m-%d")


def to_hcm(dt: datetime | None) -> datetime | None:
    """
    Chuyển một datetime sang giờ HCM.
    - Nếu dt là naive (không có tzinfo) → giả định là UTC rồi chuyển sang HCM.
    - Nếu dt là aware → chuyển sang HCM.
    - Nếu dt là None → trả về None.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(HCM_TZ)


def fmt_hcm(dt: datetime | None, fmt: str = "%H:%M %d/%m/%Y") -> str:
    """
    Format một datetime sang giờ HCM theo định dạng cho trước.
    Mặc định: '00:51 13/04/2026'
    """
    converted = to_hcm(dt)
    if converted is None:
        return "-"
    return converted.strftime(fmt)
