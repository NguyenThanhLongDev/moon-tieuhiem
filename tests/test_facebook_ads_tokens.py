from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from facebook_ads_tokens.health import (
    get_token_health_status,
    is_token_expiring_soon,
    should_refresh_record,
)
from facebook_ads_tokens.models import TokenRecord
from facebook_ads_tokens.security import mask_token
from facebook_ads_tokens.storage import load_store, save_store, upsert_record


class TestMaskToken(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(mask_token(""), "")

    def test_short(self) -> None:
        self.assertTrue(mask_token("abc").startswith("*"))

    def test_long(self) -> None:
        t = "EAAB_long_user_token_value_here"
        m = mask_token(t)
        self.assertIn("...", m)
        self.assertNotIn(t, m)


class TestStorage(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            rec = TokenRecord(
                shop_key="shop1",
                ad_account_id="123",
                access_token="secret",
                expires_at="2099-01-01T00:00:00Z",
            )
            save_store(p, [rec])
            loaded = load_store(p)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].shop_key, "shop1")
            self.assertEqual(loaded[0].access_token, "secret")

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "none.json"
            self.assertEqual(load_store(p), [])

    def test_upsert(self) -> None:
        a = TokenRecord(shop_key="s", ad_account_id="1", access_token="x")
        b = TokenRecord(shop_key="s", ad_account_id="1", access_token="y")
        rows, ins = upsert_record([], a)
        self.assertTrue(ins)
        rows, ins2 = upsert_record(rows, b)
        self.assertFalse(ins2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].access_token, "y")


class TestHealth(unittest.TestCase):
    def test_expiring_soon(self) -> None:
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        exp = (now + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        self.assertTrue(is_token_expiring_soon(exp, within_days=7, now=now))

    def test_not_expiring(self) -> None:
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        exp = (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        self.assertFalse(is_token_expiring_soon(exp, within_days=7, now=now))

    def test_health_statuses(self) -> None:
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)
        past = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        soon = (now + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        far = (now + timedelta(days=90)).isoformat().replace("+00:00", "Z")
        self.assertEqual(
            get_token_health_status(
                TokenRecord(shop_key="a", access_token="t", expires_at=past), now=now
            ),
            "expired",
        )
        self.assertEqual(
            get_token_health_status(
                TokenRecord(shop_key="a", access_token="t", expires_at=soon), now=now
            ),
            "expiring_soon",
        )
        self.assertEqual(
            get_token_health_status(
                TokenRecord(shop_key="a", access_token="t", expires_at=far), now=now
            ),
            "valid",
        )
        self.assertEqual(
            get_token_health_status(
                TokenRecord(
                    shop_key="a",
                    access_token="t",
                    expires_at=far,
                    refresh_status="refresh_failed",
                ),
                now=now,
            ),
            "refresh_failed",
        )

    def test_should_refresh_selection(self) -> None:
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        soon = (now + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        rec = TokenRecord(shop_key="s", access_token="tok", expires_at=soon)
        self.assertTrue(should_refresh_record(rec, expiring_within_days=7, now=now))
        far = (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        rec2 = TokenRecord(shop_key="s", access_token="tok", expires_at=far)
        self.assertFalse(should_refresh_record(rec2, expiring_within_days=7, now=now))


class TestExchangeMock(unittest.TestCase):
    def test_exchange_parses_expires(self) -> None:
        from facebook_ads_tokens.exchange import exchange_short_token

        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {
            "access_token": "NEWTOKEN",
            "token_type": "bearer",
            "expires_in": 3600,
        }
        with patch("facebook_ads_tokens.exchange.requests.get", return_value=fake):
            r = exchange_short_token("id", "sec", "short", graph_version="v20.0")
        self.assertEqual(r.access_token, "NEWTOKEN")
        self.assertIsNotNone(r.expires_at)


if __name__ == "__main__":
    unittest.main()
