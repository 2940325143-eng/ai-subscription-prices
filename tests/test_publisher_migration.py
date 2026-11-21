import copy
import json
import unittest

from collector.history_ledger import empty_ledger
from collector.publisher import LEDGER_PATH, PUBLISHED_PATH, build_published_snapshot, empty_overrides


class PublisherAndMigrationTests(unittest.TestCase):
    def test_migrated_published_json_integrity_and_tr_flag(self):
        pub = json.loads(PUBLISHED_PATH.read_text(encoding="utf-8"))
        self.assertEqual(pub["schemaVersion"], 2)
        self.assertLess(PUBLISHED_PATH.stat().st_size, 5 * 1024 * 1024)
        self.assertTrue(LEDGER_PATH.is_file())

        cg = pub["products"]["chatgpt"]
        self.assertEqual(len(cg["countries"]), 210)
        tr_cg = next(r for r in cg["countries"] if r["code"] == "TR")
        # TR ChatGPT go is VAT-inclusive in 18767 -> must stay pending_basis
        self.assertEqual(tr_cg["web"]["cells"]["go"]["status"], "pending_basis")
        self.assertIsNone(tr_cg["web"]["cells"]["go"]["amount"])
        self.assertEqual(tr_cg["apple"]["cells"]["plus"]["amount"], 999.99)

        gu = pub["products"]["google_ultra"]
        self.assertEqual(len(gu["countries"]), 199)
        tr_gu = next(r for r in gu["countries"] if r["code"] == "TR")
        for plan in ("ultra5x", "ultra20x"):
            cell = tr_gu["apple"]["cells"][plan]
            self.assertEqual(cell["status"], "ok")
            self.assertTrue(cell["suspectGrandfathered"])
            self.assertTrue(cell["excludeFromMinCard"])

        # Cross-currency Ghana / Iraq / Kenya remain NOT flagged
        for cc in ("GH", "IQ", "KE"):
            row = next(r for r in gu["countries"] if r["code"] == cc)
            for cell in row["apple"]["cells"].values():
                if cell.get("status") == "ok":
                    self.assertFalse(cell.get("suspectGrandfathered"))

    def test_manual_override_clears_tr_suspect_and_retains_raw_observed(self):
        pub = json.loads(PUBLISHED_PATH.read_text(encoding="utf-8"))
        overrides = empty_overrides()
        overrides["entries"]["google_ultra:TR:apple:ultra5x"] = {
            "product": "google_ultra",
            "country": "TR",
            "channel": "apple",
            "plan": "ultra5x",
            "currency": "TRY",
            "amount": 4749.99,
            "note": "Test manual calibration",
            "updatedAt": "2026-11-21T15:00:00+00:00",
        }
        rebuilt = build_published_snapshot(
            copy.deepcopy(pub["products"]),
            ledger=empty_ledger(),
            overrides=overrides,
            fx=pub["fx"],
            update_apple_ledger_for=set(),
        )
        tr_apple_5x = next(r for r in rebuilt["products"]["google_ultra"]["countries"] if r["code"] == "TR")["apple"]["cells"]["ultra5x"]
        self.assertTrue(tr_apple_5x["manualOverride"])
        self.assertEqual(tr_apple_5x["amount"], 4749.99)
        self.assertFalse(tr_apple_5x["suspectGrandfathered"])
        self.assertFalse(tr_apple_5x["excludeFromMinCard"])
        self.assertEqual(tr_apple_5x["rawObserved"]["amount"], 1989.99)


if __name__ == "__main__":
    unittest.main()
