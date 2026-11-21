import json
import unittest

from collector import chatgpt_web, google_apple, google_apps, google_web


class GoogleAndWebCollectorTests(unittest.TestCase):
    def test_chatgpt_web_prefers_exclusive_and_accepts_inclusive_fallback(self):
        payload = {
            "country_code": "TR",
            "currency_config": {
                "symbol_code": "USD",
                "symbol": "$",
                "minor_unit_exponent": 2,
                "go": {"month": {"amount": 6, "tax": "inclusive"}},
                "plus": {"month": {"amount": 22, "tax": "inclusive", "psp_override": {"amount": 20, "tax": "exclusive"}}},
                "business": {"month": {"amount": 25, "tax": "exclusive"}},
                "prolite": {"month": {"amount": 100, "tax": "exclusive"}},
                "business_prolite": {"month": {"amount": 125, "tax": "exclusive"}},
                "pro": {"month": {"amount": 200, "tax": "exclusive"}},
                "promax": {"month": {"amount": 500, "tax": "exclusive"}},
            },
        }
        ext = chatgpt_web.extract_config_prices(
            payload, country="TR", source_url=chatgpt_web.CONFIG_ENDPOINT.format(country="TR"), observed_at="2026-11-21T00:00:00+00:00"
        )
        # Inclusive-only go is accepted with taxInclusive=True
        self.assertEqual(ext["cells"]["go"]["status"], "ok")
        self.assertEqual(ext["cells"]["go"]["amount"], 6.0)
        self.assertEqual(ext["cells"]["go"]["tax"], "inclusive")
        self.assertTrue(ext["cells"]["go"]["taxInclusive"])
        # psp_override exclusive wins cleanly over inclusive month
        self.assertEqual(ext["cells"]["plus"]["amount"], 20.0)
        self.assertEqual(ext["cells"]["plus"]["tax"], "exclusive")
        self.assertFalse(ext["cells"]["plus"]["taxInclusive"])
        for plan in ("plus", "businessStandard", "pro100", "businessPremium", "pro200", "pro500"):
            self.assertEqual(ext["cells"][plan]["status"], "ok")
            self.assertEqual(ext["cells"][plan]["tax"], "exclusive")

    def test_google_web_comma_amounts_and_5x_20x_conflict_guard(self):
        self.assertEqual(google_web.parse_feed_amount("298,985.05"), 298985.05)
        self.assertEqual(google_web.parse_feed_amount("2,399,880.00"), 2399880.0)
        policy = {
            "verified": True,
            "mapping": google_web.PRICE_FIELDS,
            "sourceUrl": google_web.PAGE_URL,
            "componentUrl": "https://one.google.com/about/assets/main.js",
            "feedVersion": "pricing_2026_11_21.json",
            "observedAt": google_web.now(),
        }
        proxy = {"status": "ok", "actualCountry": "CI"}
        payload = {
            "COUNTRY_CODE": "CI",
            "CURRENCY_CODE": "USD",
            "PRICE_GEN_AI_PRO_MONTHLY": 19.99,
            "PRICE_GEN_AI_ULTRA_100_MONTHLY": 61401.0,
            "PRICE_GEN_AI_ULTRA_200_MONTHLY": 199.99,
        }
        parsed = google_web.parse_feed(
            payload, "CI", google_web.feed_url("CI", policy), policy, proxy, google_ip_country="CI"
        )
        self.assertEqual(parsed["cells"]["ultra5x"]["status"], "pending")
        self.assertIsNone(parsed["cells"]["ultra5x"]["amount"])
        self.assertTrue(parsed["cells"]["ultra5x"]["evidence"]["currencyAmountConflict"])
        self.assertEqual(parsed["cells"]["ultra20x"]["status"], "ok")
        self.assertEqual(parsed["cells"]["ultra20x"]["amount"], 199.99)

    def test_google_apple_highest_new_price_and_8_5x_guard(self):
        policy = {
            "verified": True,
            "verifiedAt": google_apple.now(),
            "monthlyOnly": True,
            "mapping": {"20TB": "ultra5x", "30TB": "ultra20x"},
            "identityText": "Google AI Ultra 5x (20 TB of storage) | Google AI Ultra 20x (30 TB of storage)",
            "periodText": "Ultra plans are billed monthly only",
        }
        payload = {
            "items": [
                {"$kind": "textPair", "leadingText": "Google AI Ultra (30 TB)", "trailingText": "149,99 €"},
                {"$kind": "textPair", "leadingText": "Google AI Ultra (30 TB)", "trailingText": "219,99 €"},
            ]
        }
        html = f'<script>"priceCurrency":"EUR"</script><link rel="canonical" href="https://apps.apple.com/sk/app/google-gemini/id6477489729"><script id="serialized-server-data">{json.dumps(payload)}</script>'
        products = google_apple.parse_storefront(
            html,
            app_id="6477489729",
            app_name="Google Gemini",
            country="SK",
            source_url="https://apps.apple.com/sk/app/google-gemini/id6477489729",
            policy=policy,
        )
        cell = google_apple.select_candidates(products, "ultra20x")
        self.assertEqual(cell["status"], "ok")
        self.assertEqual(cell["amount"], 219.99)
        self.assertEqual(cell["evidence"]["supersededGrandfatheredPrices"], [149.99])

    def test_google_apps_helpers(self):
        ordered = google_apps.order_app_map({"999": "Other", "1451784328": "Google One", "6477489729": "Google Gemini"})
        self.assertEqual(list(ordered.keys()), ["6477489729", "1451784328", "999"])
        self.assertEqual(google_apps.count_iap_pairs('<script id="serialized-server-data">{"$kind":"textPair","leadingText":"A","trailingText":"$1"}</script>'), 1)


if __name__ == "__main__":
    unittest.main()
