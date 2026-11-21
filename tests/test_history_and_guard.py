import unittest

from collector.anomaly_guard import apply_grandfathered_guard
from collector.history_ledger import empty_ledger, record_and_resolve_apple_cell


class HistoryLedgerAndGuardTests(unittest.TestCase):
    def _ok_cell(self, plan, currency, amount, observed_at, *, app_id="1451784328", app_name="Google One"):
        return {
            "plan": plan,
            "status": "ok",
            "amount": amount,
            "currency": currency,
            "displayPrice": f"{currency} {amount}",
            "period": "month",
            "appId": app_id,
            "appName": app_name,
            "sourceUrl": f"https://apps.apple.com/tr/app/id{app_id}",
            "sourceProductName": f"Product {plan}",
            "observedAt": observed_at,
            "identityVerified": True,
            "periodVerified": True,
            "regionVerified": True,
            "priceScope": "apple_public_monthly_iap",
            "evidence": {"requestCountry": "TR", "storefrontCountry": "TR"},
        }

    def test_ledger_retains_higher_price_when_dropped_out_within_90_days(self):
        ledger = empty_ledger()
        day1 = self._ok_cell("ultra5x", "TRY", 4749.99, "2026-11-01T04:00:00+00:00")
        res1 = record_and_resolve_apple_cell(
            ledger, product="google_ultra", country="TR", plan="ultra5x", live_cell=day1, now_at="2026-11-01T04:00:00+00:00"
        )
        self.assertEqual(res1["amount"], 4749.99)
        self.assertFalse(res1["historyRetained"])

        # Day 10: higher SKU drops out of Top 10, leaving only lower grandfathered price!
        day10_gf = self._ok_cell("ultra5x", "TRY", 1989.99, "2026-11-11T04:00:00+00:00")
        res10 = record_and_resolve_apple_cell(
            ledger, product="google_ultra", country="TR", plan="ultra5x", live_cell=day10_gf, now_at="2026-11-11T04:00:00+00:00"
        )
        self.assertEqual(res10["amount"], 4749.99)
        self.assertTrue(res10["historyRetained"])
        self.assertFalse(res10["historyStale"])
        self.assertFalse(res10["excludeFromMinCard"])
        self.assertIn(1989.99, res10["evidence"]["supersededGrandfatheredPrices"])

        # Day 20: Ultra disappears completely from Top 10 (pending) -> still retains 4749.99!
        day20_empty = {"plan": "ultra5x", "status": "pending", "amount": None, "currency": None}
        res20 = record_and_resolve_apple_cell(
            ledger, product="google_ultra", country="TR", plan="ultra5x", live_cell=day20_empty, now_at="2026-11-21T04:00:00+00:00"
        )
        self.assertEqual(res20["status"], "ok")
        self.assertEqual(res20["amount"], 4749.99)
        self.assertTrue(res20["historyRetained"])
        self.assertFalse(res20["historyStale"])

    def test_ledger_marks_stale_after_90_days_if_missing_and_allows_price_cut_if_live(self):
        ledger = empty_ledger()
        day1 = self._ok_cell("ultra5x", "EUR", 219.99, "2026-01-01T04:00:00+00:00")
        record_and_resolve_apple_cell(
            ledger, product="google_ultra", country="SK", plan="ultra5x", live_cell=day1, now_at="2026-01-01T04:00:00+00:00"
        )
        # Day 120: missing -> stale & excluded from minimum card
        missing = {"plan": "ultra5x", "status": "pending", "amount": None, "currency": None}
        res_stale = record_and_resolve_apple_cell(
            ledger, product="google_ultra", country="SK", plan="ultra5x", live_cell=missing, now_at="2026-05-01T04:00:00+00:00"
        )
        self.assertEqual(res_stale["amount"], 219.99)
        self.assertTrue(res_stale["historyRetained"])
        self.assertTrue(res_stale["historyStale"])
        self.assertTrue(res_stale["excludeFromMinCard"])

        # Day 121: legitimate live lower price appears -> accepted!
        live_cut = self._ok_cell("ultra5x", "EUR", 179.99, "2026-05-02T04:00:00+00:00")
        res_live = record_and_resolve_apple_cell(
            ledger, product="google_ultra", country="SK", plan="ultra5x", live_cell=live_cut, now_at="2026-05-02T04:00:00+00:00"
        )
        self.assertEqual(res_live["amount"], 179.99)
        self.assertFalse(res_live["historyRetained"])

    def test_same_currency_grandfathered_guard(self):
        row = {
            "web": {
                "cells": {
                    "ultra5x": {"status": "ok", "currency": "TRY", "amount": 4749.99},
                    "ultra20x": {"status": "ok", "currency": "GHS", "amount": 2900.0},
                }
            },
            "apple": {
                "cells": {
                    "ultra5x": {"status": "ok", "currency": "TRY", "amount": 1989.99},
                    "ultra20x": {"status": "ok", "currency": "USD", "amount": 199.99},
                }
            },
        }
        apply_grandfathered_guard(row, ("ultra5x", "ultra20x"))
        self.assertTrue(row["apple"]["cells"]["ultra5x"]["suspectGrandfathered"])
        self.assertTrue(row["apple"]["cells"]["ultra5x"]["excludeFromMinCard"])
        self.assertAlmostEqual(row["apple"]["cells"]["ultra5x"]["suspectRatio"], 0.4189, places=3)

        # Cross-currency (GHS vs USD) must NEVER be flagged
        self.assertFalse(row["apple"]["cells"]["ultra20x"]["suspectGrandfathered"])
        self.assertFalse(row["apple"]["cells"]["ultra20x"]["excludeFromMinCard"])

        # Manual override clears suspect flag
        row["apple"]["cells"]["ultra5x"]["manualOverride"] = True
        apply_grandfathered_guard(row, ("ultra5x", "ultra20x"))
        self.assertFalse(row["apple"]["cells"]["ultra5x"]["suspectGrandfathered"])
        self.assertFalse(row["apple"]["cells"]["ultra5x"]["excludeFromMinCard"])


if __name__ == "__main__":
    unittest.main()
