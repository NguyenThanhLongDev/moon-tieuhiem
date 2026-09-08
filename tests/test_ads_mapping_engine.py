from __future__ import annotations

from datetime import date

from modules.ads_mapping.engine import pick_mapping_for_raw, sort_rules_for_display


def test_priority_order_name_pattern_lower_wins():
    raw = {
        "id": 1,
        "account_name": "Shop ABC Ads",
        "fb_ad_account_id": "12345",
        "shop_key": "sk",
        "shop_name": "Shop",
    }
    rules = [
        {
            "id": 2,
            "is_active": True,
            "match_level": "name_pattern",
            "name_pattern": "ABC",
            "product_id": 99,
            "priority": 50,
            "fb_ad_account_id": None,
            "effective_from": None,
            "effective_to": None,
            "campaign_id": None,
            "campaign_name": None,
            "adset_id": None,
            "adset_name": None,
            "ad_id": None,
            "ad_name": None,
        },
        {
            "id": 1,
            "is_active": True,
            "match_level": "name_pattern",
            "name_pattern": "ABC",
            "product_id": 88,
            "priority": 100,
            "fb_ad_account_id": None,
            "effective_from": None,
            "effective_to": None,
            "campaign_id": None,
            "campaign_name": None,
            "adset_id": None,
            "adset_name": None,
            "ad_id": None,
            "ad_name": None,
        },
    ]
    method, pid, rid, conf, st, _ = pick_mapping_for_raw(raw, rules, metric_date=date(2026, 4, 1))
    assert method == "name_pattern"
    assert pid == 99
    assert rid == 2
    assert st == "low_confidence"
    assert conf == 65.0


def test_ad_level_before_name_pattern():
    raw = {
        "id": 1,
        "ad_id": "777",
        "ad_name": "",
        "account_name": "x",
        "fb_ad_account_id": "1",
        "shop_key": "sk",
        "shop_name": "S",
    }
    rules = [
        {
            "id": 10,
            "is_active": True,
            "match_level": "name_pattern",
            "name_pattern": "x",
            "product_id": 1,
            "priority": 1,
            "fb_ad_account_id": None,
            "effective_from": None,
            "effective_to": None,
            "campaign_id": None,
            "campaign_name": None,
            "adset_id": None,
            "adset_name": None,
            "ad_id": None,
            "ad_name": None,
        },
        {
            "id": 11,
            "is_active": True,
            "match_level": "ad",
            "ad_id": "777",
            "product_id": 2,
            "priority": 999,
            "fb_ad_account_id": None,
            "effective_from": None,
            "effective_to": None,
            "campaign_id": None,
            "campaign_name": None,
            "adset_id": None,
            "adset_name": None,
            "ad_name": None,
            "name_pattern": None,
        },
    ]
    method, pid, rid, conf, st, _ = pick_mapping_for_raw(raw, rules, metric_date=date(2026, 4, 1))
    assert method == "ad_id"
    assert pid == 2
    assert rid == 11
    assert st == "mapped"
    assert conf == 100.0


def test_sort_rules_for_display():
    rules = [{"id": 3, "priority": 10}, {"id": 1, "priority": 10}, {"id": 2, "priority": 5}]
    s = sort_rules_for_display(rules)
    assert [r["id"] for r in s] == [2, 1, 3]
