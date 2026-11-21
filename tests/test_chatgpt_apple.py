import json
import unittest

from collector import chatgpt_apple


class ChatGPTAppleTests(unittest.TestCase):
    def test_triple_candidate_isolates_annual_and_picks_highest_monthly(self):
        candidates = [
            {"sourceProductName": "ChatGPT Plus", "displayPrice": "$19.99", "amount": 19.99, "currency": "USD"},
            {"sourceProductName": "ChatGPT Plus", "displayPrice": "$24.99", "amount": 24.99, "currency": "USD"},
            {"sourceProductName": "ChatGPT Plus", "displayPrice": "$249.99", "amount": 249.99, "currency": "USD"},
        ]
        cell, annotated = chatgpt_apple.select_plan_candidates(candidates, "plus", currency="USD")
        self.assertEqual(cell["status"], "ok")
        self.assertEqual(cell["amount"], 24.99)
        self.assertEqual(cell["evidence"]["excludedAnnualPrices"], [249.99])
        self.assertEqual(cell["evidence"]["supersededGrandfatheredPrices"], [19.99])
        gf = [c for c in annotated if c["amount"] == 19.99][0]
        self.assertTrue(gf.get("excludedAsGrandfathered"))
        ann = [c for c in annotated if c["amount"] == 249.99][0]
        self.assertTrue(ann.get("excludedAsAnnual"))

    def test_standard_monthly_annual_pair_tj_borderline_8_7x(self):
        # Verified real TJ ratio = 200.0 / 22.99 = 8.699 >= 8.5
        candidates = [
            {"sourceProductName": "ChatGPT Plus", "displayPrice": "$22.99", "amount": 22.99, "currency": "USD"},
            {"sourceProductName": "ChatGPT Plus", "displayPrice": "$200.00", "amount": 200.0, "currency": "USD"},
        ]
        cell, _ = chatgpt_apple.select_plan_candidates(candidates, "plus", currency="USD")
        self.assertEqual(cell["amount"], 22.99)
        self.assertEqual(cell["evidence"]["excludedAnnualPrices"], [200.0])

    def test_explicit_annual_and_promo_excluded(self):
        candidates = [
            {"sourceProductName": "ChatGPT Plus Intro Offer", "displayPrice": "$9.99", "amount": 9.99, "currency": "USD"},
            {"sourceProductName": "ChatGPT Plus", "displayPrice": "$19.99", "amount": 19.99, "currency": "USD"},
            {"sourceProductName": "ChatGPT Plus Annual", "displayPrice": "$199.99", "amount": 199.99, "currency": "USD"},
        ]
        cell, annotated = chatgpt_apple.select_plan_candidates(candidates, "plus", currency="USD")
        self.assertEqual(cell["amount"], 19.99)
        self.assertEqual(sum(1 for c in annotated if c.get("isPromotional")), 1)
        self.assertEqual(sum(1 for c in annotated if c.get("isAnnual")), 1)

    def test_modern_pro_100_preferred_over_legacy_5x(self):
        candidates = [
            {"sourceProductName": "ChatGPT Pro 5x", "displayPrice": "$80.00", "amount": 80.0, "currency": "USD"},
            {"sourceProductName": "ChatGPT Pro 100", "displayPrice": "$100.00", "amount": 100.0, "currency": "USD"},
        ]
        cell, _ = chatgpt_apple.select_plan_candidates(candidates, "pro100", currency="USD")
        self.assertEqual(cell["amount"], 100.0)
        self.assertEqual(cell["sourceProductName"], "ChatGPT Pro 100")

    def test_parse_storefront_serialized_json_tr_currency(self):
        payload = {
            "offers": [
                {"$kind": "textPair", "leadingText": "ChatGPT Go", "trailingText": "₺249,99"},
                {"$kind": "textPair", "leadingText": "ChatGPT Plus", "trailingText": "₺999,99"},
                {"$kind": "textPair", "leadingText": "ChatGPT Plus", "trailingText": "₺9.999,99"},
                {"$kind": "textPair", "leadingText": "ChatGPT Pro 100", "trailingText": "₺5.299,99"},
                {"$kind": "textPair", "leadingText": "ChatGPT Pro 200", "trailingText": "₺9.999,99"},
                {"$kind": "textPair", "leadingText": "ChatGPT Pro 500", "trailingText": "₺26.499,00"},
            ]
        }
        html = f'<script>"priceCurrency":"TRY"</script><script id="serialized-server-data">{json.dumps(payload)}</script>'
        parsed = chatgpt_apple.parse_storefront(
            html, country="TR", source_url="https://apps.apple.com/tr/app/chatgpt/id6448311069"
        )
        self.assertEqual(parsed["currency"], "TRY")
        self.assertEqual(parsed["cells"]["go"]["amount"], 249.99)
        self.assertEqual(parsed["cells"]["plus"]["amount"], 999.99)
        self.assertEqual(parsed["cells"]["pro100"]["amount"], 5299.99)
        self.assertEqual(parsed["cells"]["pro200"]["amount"], 9999.99)
        self.assertEqual(parsed["cells"]["pro500"]["amount"], 26499.0)


if __name__ == "__main__":
    unittest.main()
