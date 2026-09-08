from __future__ import annotations

import unittest

from .calculator import calculate_salary


def _config(deduct_shipping_fee: bool, vat_rate: float = 0.1):
    return {
        "id": 1,
        "code": "cfg",
        "name": "cfg",
        "branch_code": "ALL",
        "default_vat_rate": vat_rate,
        "deduct_shipping_fee": deduct_shipping_fee,
        "stop_ads_threshold": 50000.0,
    }


TIERS = [
    {"min_ads_per_order": 0, "max_ads_per_order": 30000, "kpi_percent": 0.03, "is_stop_run": False, "sort_order": 1},
    {"min_ads_per_order": 30000.00001, "max_ads_per_order": 40000, "kpi_percent": 0.025, "is_stop_run": False, "sort_order": 2},
    {"min_ads_per_order": 40000.00001, "max_ads_per_order": 50000, "kpi_percent": 0.014, "is_stop_run": False, "sort_order": 3},
    {"min_ads_per_order": 50000.00001, "max_ads_per_order": None, "kpi_percent": 0.0, "is_stop_run": True, "sort_order": 4},
]


class SalaryCalculatorTests(unittest.TestCase):
    def test_tier_30k(self):
        result = calculate_salary(
            {"revenue_gross": 1100000, "ads_cost": 300000, "orders_count": 10, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(True),
            TIERS,
        )
        self.assertAlmostEqual(result.ads_per_order, 30000.0, places=6)
        self.assertAlmostEqual(result.kpi_percent, 0.03, places=6)

    def test_tier_40k(self):
        result = calculate_salary(
            {"revenue_gross": 1100000, "ads_cost": 400000, "orders_count": 10, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(True),
            TIERS,
        )
        self.assertAlmostEqual(result.kpi_percent, 0.025, places=6)

    def test_tier_50k(self):
        result = calculate_salary(
            {"revenue_gross": 1100000, "ads_cost": 500000, "orders_count": 10, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(True),
            TIERS,
        )
        self.assertAlmostEqual(result.kpi_percent, 0.014, places=6)
        self.assertFalse(result.stop_ads)

    def test_tier_gt_50k_stop(self):
        result = calculate_salary(
            {"revenue_gross": 1100000, "ads_cost": 510000, "orders_count": 10, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(True),
            TIERS,
        )
        self.assertTrue(result.stop_ads)
        self.assertAlmostEqual(result.kpi_percent, 0.0, places=6)

    def test_vat_8_percent(self):
        result = calculate_salary(
            {"revenue_gross": 1080000, "vat_rate": 0.08, "ads_cost": 0, "orders_count": 1, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(True),
            TIERS,
        )
        self.assertAlmostEqual(result.revenue_net, 1000000.0, places=2)

    def test_vat_10_percent(self):
        result = calculate_salary(
            {"revenue_gross": 1100000, "vat_rate": 0.10, "ads_cost": 0, "orders_count": 1, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(True),
            TIERS,
        )
        self.assertAlmostEqual(result.revenue_net, 1000000.0, places=2)

    def test_deduct_ship(self):
        result = calculate_salary(
            {"revenue_gross": 1100000, "vat_rate": 0.10, "shipping_fee": 50000, "ads_cost": 0, "orders_count": 1, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(True),
            TIERS,
        )
        self.assertAlmostEqual(result.revenue_for_kpi, 950000.0, places=2)

    def test_not_deduct_ship(self):
        result = calculate_salary(
            {"revenue_gross": 1100000, "vat_rate": 0.10, "shipping_fee": 50000, "ads_cost": 0, "orders_count": 1, "base_salary": 0, "allowance": 0, "penalty": 0, "advance": 0},
            _config(False),
            TIERS,
        )
        self.assertAlmostEqual(result.revenue_for_kpi, 1000000.0, places=2)


if __name__ == "__main__":
    unittest.main()
