from datetime import datetime, timedelta


def parse_target_date(text: str):
    text = (text or "").lower().strip()
    today = datetime.today()

    if "hom nay" in text or "hôm nay" in text:
        return today.strftime("%Y-%m-%d")

    if "hom qua" in text or "hôm qua" in text:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    if "hom kia" in text or "hôm kia" in text:
        return (today - timedelta(days=2)).strftime("%Y-%m-%d")

    for part in text.replace(",", " ").split():
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d%m%Y"):
            try:
                return datetime.strptime(part, fmt).strftime("%Y-%m-%d")
            except Exception:
                pass

        try:
            return datetime.strptime(part, "%d/%m").replace(year=today.year).strftime("%Y-%m-%d")
        except Exception:
            pass

    return None
