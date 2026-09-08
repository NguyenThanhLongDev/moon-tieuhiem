from __future__ import annotations

# Align with dashboard ads KPI (web_app fb VAT helpers).
ADS_VAT_RATE = 0.113
ADS_COST_MULTIPLIER = 1.0 + ADS_VAT_RATE

MATCH_LEVELS_ORDER = ("ad", "adset", "campaign", "name_pattern")

METHOD_BY_LEVEL = {
    "ad": "ad_id",
    "adset": "adset_id",
    "campaign": "campaign_id",
    "name_pattern": "name_pattern",
}
