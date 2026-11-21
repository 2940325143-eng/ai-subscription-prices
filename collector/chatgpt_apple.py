"""ChatGPT App Store IAP collector (new-price priority + annual isolation).

Reads public App Store storefront offers for ChatGPT (id6448311069). Upgrades
the legacy min() selection to:
  1. Exclude explicit promotional and annual/multi-month items.
  2. Prefer current Pro 100 / 200 / 500 names over legacy Pro 5x / 10x / 20x.
  3. Isolate unlabeled annual SKUs (>= 8.5x lowest monthly candidate without
     explicit monthly period metadata) BEFORE selecting the monthly winner.
  4. Select the HIGHEST remaining valid monthly price (max) so post-hike
     new-subscriber prices beat grandfathered renewal SKUs.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
import html as html_module
import json
import math
import re
import time
import unicodedata
from urllib.parse import urlparse

from curl_cffi import requests

from collector.common import now_iso, parse_apple_display_amount

APP_ID = "6448311069"
APP_NAME = "ChatGPT"
PLANS = ("go", "plus", "pro100", "pro200", "pro500")
ANNUAL_ISOLATION_RATIO = 8.5

PRODUCT_NAMES = {
    "ChatGPT Go": "go",
    "ChatGPT Plus": "plus",
    "ChatGPT Pro 100": "pro100",
    "ChatGPT Pro 200": "pro200",
    "ChatGPT Pro 500": "pro500",
    # Former labels retained only as fallback when current labels are absent
    "ChatGPT Pro 5x": "pro100",
    "ChatGPT Pro 10x": "pro200",
    "ChatGPT Pro 20x": "pro200",
}
CURRENT_PRO_PLANS = frozenset({"pro100", "pro200", "pro500"})


def clean_text(value) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def identify_product_plan(name: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", clean_text(name))
    canonical = {k.lower(): v for k, v in PRODUCT_NAMES.items()}
    if normalized.lower() in canonical:
        return canonical[normalized.lower()]
    # Also match suffixed variants like "ChatGPT Plus (Monthly)" / "ChatGPT Plus Annual"
    base_match = re.match(
        r"^(ChatGPT\s+(?:Go|Plus|Pro\s*(?:5x|10x|20x|100|200|500)))(?:\s*\([^)]*\)|\s+Monthly|\s+Annual|\s+Yearly)?$",
        normalized,
        flags=re.I,
    )
    if base_match:
        hit = canonical.get(re.sub(r"\s+", " ", base_match.group(1)).lower())
        if hit:
            return hit
    pro_match = re.fullmatch(
        r"ChatGPT\s+Pro\s*(?:US\s*)?\$?\s*(100|200|500)(?:\s*(?:USD|美元))?",
        normalized,
        flags=re.I,
    )
    return f"pro{pro_match.group(1)}" if pro_match else None


def detect_period(raw) -> str | None:
    if isinstance(raw, dict):
        unit = str(raw.get("unit") or raw.get("periodUnit") or "").lower()
        count = raw.get("numberOfUnits", raw.get("value", 1))
        if unit in {"month", "monthly"} and count in {1, "1"}:
            return "month"
        if unit in {"year", "yearly"}:
            return "year"
        return "other" if unit else None
    text = unicodedata.normalize("NFKC", str(raw or "")).lower()
    if re.search(r"\b(?:[2-9]|1[0-2])\s*months?\b|季度|半年", text):
        return "other"
    if re.search(r"\b(yearly|annually|annual|year|p1y)\b|每年|年付|年度", text):
        return "year"
    if re.search(r"\b(monthly|month|p1m)\b|每月|月付|月度", text):
        return "month"
    return None


def extract_text_pairs(page_html: str) -> list[dict]:
    pairs: list[dict] = []
    for match in re.finditer(
        r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>',
        page_html,
        re.S | re.I,
    ):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue

        def walk(node):
            if isinstance(node, dict):
                if (
                    node.get("$kind") == "textPair"
                    and isinstance(node.get("leadingText"), str)
                    and isinstance(node.get("trailingText"), str)
                ):
                    pairs.append({
                        "name": node["leadingText"],
                        "displayPrice": node["trailingText"],
                        "sourcePeriod": node.get("subscriptionPeriod") or node.get("billingPeriod") or node.get("period"),
                        "productId": node.get("productId") or node.get("adamId"),
                    })
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(data)

    for name, price in re.findall(
        r"<span(?:\s[^>]*)?>\s*(ChatGPT\s+(?:Go|Plus|Pro(?:\b|(?=\d))[^<]*))\s*</span>\s*<span(?:\s[^>]*)?>(.*?)</span>",
        page_html,
        re.I | re.S,
    ):
        pairs.append({"name": clean_text(name), "displayPrice": clean_text(price), "sourcePeriod": None, "productId": None})

    unique: dict[tuple[str, str, str], dict] = {}
    for pair in pairs:
        name = clean_text(pair["name"])
        price = clean_text(pair["displayPrice"])
        period_tag = str(pair.get("sourcePeriod") or "")
        key = (name, price, period_tag)
        if key not in unique or pair.get("productId"):
            unique[key] = {**pair, "name": name, "displayPrice": price}
    return list(unique.values())


def select_plan_candidates(
    candidates: list[dict],
    plan: str,
    *,
    currency: str | None = None,
    annual_ratio: float = ANNUAL_ISOLATION_RATIO,
) -> tuple[dict, list[dict]]:
    """Select the new-subscriber monthly price from ChatGPT Apple candidates."""
    observed = now_iso()
    pool = []
    annotated = []
    for raw in candidates:
        item = copy.deepcopy(raw)
        name = clean_text(item.get("sourceProductName") or "")
        amt = item.get("amount")
        cand_currency = item.get("currency") or currency
        if type(amt) not in (int, float) or not math.isfinite(amt) or amt <= 0:
            item["excludedReason"] = "invalid_amount"
            annotated.append(item)
            continue
        promotional = bool(
            re.search(r"\b(trial|introductory|intro|promo(?:tional)?|discount|offer|student|legacy|old)\b|试用|优惠|折扣|学生|旧版", name, re.I)
        )
        explicit_period = detect_period(item.get("sourcePeriod")) or detect_period(name)
        if promotional:
            item.update(isPromotional=True, excludedReason="promotional_offer")
            annotated.append(item)
            continue
        if explicit_period in {"year", "other"}:
            item.update(isAnnual=True, excludedReason="explicit_annual_or_multi_month")
            annotated.append(item)
            continue
        item.setdefault("currency", cand_currency)
        item["sourcePeriod"] = explicit_period
        pool.append(item)
        annotated.append(item)

    if not pool:
        return (
            {
                "plan": plan,
                "status": "pending",
                "amount": None,
                "currency": None,
                "period": None,
                "observedAt": observed,
                "reason": "Apple 公开列表未露出可核验月付商品",
                "candidates": annotated,
            },
            annotated,
        )

    # Prefer modern Pro 100 / 200 / 500 labels when present
    if plan in CURRENT_PRO_PLANS:
        modern = [c for c in pool if re.search(r"\b(?:100|200|500)\b", str(c.get("sourceProductName") or ""))]
        if modern:
            modern_set = {id(c) for c in modern}
            for c in annotated:
                if c in pool and id(c) not in modern_set:
                    c["excludedReason"] = "superseded_legacy_pro_label"
            pool = modern

    min_amount = min(float(c["amount"]) for c in pool)
    excluded_annual: list[float] = []
    monthly_pool: list[dict] = []
    for c in pool:
        amt = float(c["amount"])
        if min_amount > 0 and amt / min_amount >= annual_ratio and c.get("sourcePeriod") != "month":
            excluded_annual.append(amt)
            c["isAnnual"] = True
            c["excludedAsAnnual"] = True
            c["excludedReason"] = f"unlabeled_annual_suspect (>={annual_ratio:g}x)"
        else:
            monthly_pool.append(c)

    if not monthly_pool:
        monthly_pool = pool

    # Select HIGHEST remaining valid monthly amount (new subscriber post-hike price)
    winner = copy.deepcopy(max(monthly_pool, key=lambda c: float(c["amount"])))
    win_amount = float(winner["amount"])
    superseded = sorted({float(c["amount"]) for c in monthly_pool if float(c["amount"]) < win_amount})

    for item in annotated:
        amt = item.get("amount")
        if (
            type(amt) in (int, float)
            and float(amt) in superseded
            and not item.get("isAnnual")
            and not item.get("isPromotional")
        ):
            item["excludedAsGrandfathered"] = True
            item["supersededByAmount"] = win_amount

    reason = (
        "已隔离年付候选项并按苹果涨价机制取最高月付新价（已排除低价旧续订价）"
        if superseded
        else "已核验 App Store 当地月付商品（已隔离年付/优惠项）"
        if excluded_annual
        else "已核验 App Store 当地月付商品"
    )
    ev = copy.deepcopy(winner.get("evidence") or {})
    ev.update({
        "priceSelection": "highest_new_subscriber_monthly",
        "excludedAnnualPrices": sorted(set(excluded_annual)),
        "supersededGrandfatheredPrices": superseded,
    })

    winner_cell = {
        "plan": plan,
        "status": "ok",
        "amount": win_amount,
        "currency": winner.get("currency") or currency,
        "displayPrice": winner.get("displayPrice"),
        "sourceProductName": winner.get("sourceProductName"),
        "period": "month",
        "appId": winner.get("appId") or APP_ID,
        "appName": winner.get("appName") or APP_NAME,
        "sourceUrl": winner.get("sourceUrl"),
        "observedAt": winner.get("observedAt") or observed,
        "identityVerified": True,
        "periodVerified": True,
        "regionVerified": True,
        "priceScope": "apple_public_monthly_iap",
        "reason": reason,
        "evidence": ev,
        "candidates": annotated,
    }
    return winner_cell, annotated


def parse_storefront(page_html: str, *, country: str, source_url: str) -> dict:
    currencies = set(re.findall(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', page_html))
    if len(currencies) != 1:
        raise ValueError("priceCurrency missing or ambiguous")
    currency = next(iter(currencies))
    observed = now_iso()

    candidates_by_plan: dict[str, list[dict]] = {p: [] for p in PLANS}
    for raw in extract_text_pairs(page_html):
        plan = identify_product_plan(raw["name"])
        if not plan or plan not in candidates_by_plan:
            continue
        amount = parse_apple_display_amount(raw["displayPrice"], currency)
        candidates_by_plan[plan].append({
            "sourceProductName": raw["name"],
            "displayPrice": raw["displayPrice"],
            "amount": amount,
            "currency": currency,
            "sourcePeriod": detect_period(raw.get("sourcePeriod")) or detect_period(raw["name"]),
            "productId": raw.get("productId"),
            "appId": APP_ID,
            "appName": APP_NAME,
            "sourceUrl": source_url,
            "observedAt": observed,
        })

    cells: dict[str, dict] = {}
    all_candidates: dict[str, list[dict]] = {}
    for plan in PLANS:
        cell, annotated = select_plan_candidates(candidates_by_plan[plan], plan, currency=currency)
        cells[plan] = cell
        all_candidates[plan] = annotated

    return {"currency": currency, "cells": cells, "candidates": all_candidates}


def fetch_country(
    code: str,
    *,
    stop=None,
    emit=lambda m: None,
    timeout: int = 12,
    limiter=None,
    session=None,
) -> dict:
    country = str(code).upper()
    url = f"https://apps.apple.com/{country.lower()}/app/chatgpt/id{APP_ID}"
    attempt = 1
    max_attempts = 2
    last_http = None
    last_error = None
    while attempt <= max_attempts:
        if stop and stop.is_set():
            return {
                "countryCode": country,
                "status": "error",
                "reason": "任务已取消",
                "cells": {p: {"plan": p, "status": "error", "amount": None, "currency": None} for p in PLANS},
            }
        try:
            headers = {"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache"}
            slot_ctx = limiter.slot(stop=stop) if limiter is not None else nullcontext(True)
            with slot_ctx as acquired:
                if not acquired:
                    return {
                        "countryCode": country,
                        "status": "error",
                        "reason": "任务已取消",
                        "cells": {p: {"plan": p, "status": "error", "amount": None, "currency": None} for p in PLANS},
                    }
                if session is not None:
                    resp = session.get(url, timeout=min(15, timeout), allow_redirects=True, headers=headers)
                else:
                    with requests.Session(impersonate="chrome", trust_env=False) as s:
                        resp = s.get(url, timeout=min(15, timeout), allow_redirects=True, headers=headers)
            last_http = resp.status_code
            parsed = urlparse(resp.url)
            pieces = parsed.path.strip("/").split("/")
            final_sf = pieces[0].upper() if pieces and pieces[0] else None
            if resp.status_code == 404:
                if limiter is not None:
                    limiter.note_success()
                return {
                    "countryCode": country,
                    "status": "unavailable",
                    "availabilityConfirmed": True,
                    "httpStatus": 404,
                    "observedAt": now_iso(),
                    "reason": "Apple 确认该地区未上架 ChatGPT（HTTP 404）",
                    "cells": {
                        p: {"plan": p, "status": "unavailable", "availabilityConfirmed": True, "amount": None, "currency": None, "reason": "未上架"}
                        for p in PLANS
                    },
                }
            if resp.status_code in {429, 503}:
                raw_ra = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                try:
                    ra = max(0.0, min(15.0, float(raw_ra))) if raw_ra else None
                except ValueError:
                    ra = None
                if limiter is not None:
                    limiter.note_rate_limit(ra)
                max_attempts = 4
                raise ValueError(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            if limiter is not None:
                limiter.note_success()
            if parsed.hostname != "apps.apple.com" or final_sf != country:
                return {
                    "countryCode": country,
                    "status": "pending",
                    "httpStatus": resp.status_code,
                    "finalStorefront": final_sf,
                    "observedAt": now_iso(),
                    "reason": f"Apple 跨区重定向至 {final_sf or '未知'}；不采用跨区价格",
                    "cells": {
                        p: {"plan": p, "status": "pending", "amount": None, "currency": None, "reason": f"跨区重定向至 {final_sf}"}
                        for p in PLANS
                    },
                }
            parsed_data = parse_storefront(resp.text, country=country, source_url=resp.url)
            ok_count = sum(c["status"] == "ok" for c in parsed_data["cells"].values())
            emit(f"ChatGPT Apple {country}：命中 {ok_count}/{len(PLANS)} 档 ({parsed_data['currency']})")
            return {
                "countryCode": country,
                "status": "ok" if ok_count == len(PLANS) else "partial" if ok_count else "pending",
                "httpStatus": resp.status_code,
                "observedAt": now_iso(),
                "currency": parsed_data["currency"],
                "sourceUrl": resp.url,
                "cells": parsed_data["cells"],
            }
        except Exception as err:
            last_error = type(err).__name__
            if attempt < max_attempts:
                delay = 0.5 if limiter is not None else min(6.0, 0.8 * attempt)
                if stop:
                    stop.wait(delay)
                else:
                    time.sleep(delay)
        attempt += 1
    return {
        "countryCode": country,
        "status": "error",
        "httpStatus": last_http,
        "observedAt": now_iso(),
        "reason": f"ChatGPT Apple 请求失败（{last_error or last_http}）",
        "cells": {
            p: {"plan": p, "status": "error", "amount": None, "currency": None, "reason": "请求失败"}
            for p in PLANS
        },
    }
