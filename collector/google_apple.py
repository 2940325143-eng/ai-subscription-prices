"""Read Google AI Ultra's public Apple IAP offers with explicit evidence.

Ported cleanly from google-ai-ultra-prices/apple_collector.py. Always scans all
discovered IAP-bearing Google LLC apps per storefront, handles price-increase
duality by picking the highest new-subscriber monthly price (never grandfathered
renewal prices), and guards against unlabeled annual offers (>=8.5x without
monthly period metadata).
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
from datetime import datetime, timezone
from decimal import InvalidOperation
import html
import json
import math
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.parse import urlparse

from curl_cffi import requests

from collector.common import DATA_DIR, now_iso, parse_apple_display_amount, read_json, write_json
from collector.google_apps import (
    APPLE_APPS,
    GOOGLE_DEVELOPER_ID,
    AppleRateLimiter,
    allowed_apple_apps,
    discover_google_apps,
    order_app_map,
)

PLANS = ("ultra5x", "ultra20x")
APPS = APPLE_APPS
IDENTITY_URL = "https://support.google.com/googleone/answer/16476748?hl=en"
MONTHLY_URL = "https://one.google.com/about/google-ai-plans/?gl=US&hl=en"
APPLE_IDENTITY = IDENTITY_URL
APPLE_PERIOD = MONTHLY_URL
POLICY_PATH = DATA_DIR / "apple-policy.json"
POLICY_MAX_AGE = 86400
POLICY_LOCK = threading.RLock()
_POLICY = None


def now() -> str:
    return now_iso()


def clean_text(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip()


def visible_text(value: str) -> str:
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", value, flags=re.I | re.S)
    return unicodedata.normalize("NFKC", clean_text(value))


def policy_fresh(policy, *, at=None, max_age: int = POLICY_MAX_AGE) -> bool:
    try:
        observed = datetime.fromisoformat(at or now())
        fetched = datetime.fromisoformat(policy["verifiedAt"])
        return bool(observed.tzinfo and fetched.tzinfo and 0 <= (observed - fetched).total_seconds() <= max_age)
    except (TypeError, ValueError, KeyError):
        return False


def policy_at_quote(policy_date, quote_date) -> bool:
    try:
        policy = datetime.fromisoformat(policy_date or "")
        quote = datetime.fromisoformat(quote_date or "")
        return bool(policy.tzinfo and quote.tzinfo and 0 <= (quote - policy).total_seconds() <= 86400)
    except (TypeError, ValueError):
        return False


def parse_policy(identity_html: str, monthly_html: str, *, verified_at=None) -> dict:
    identity = visible_text(identity_html)
    monthly = visible_text(monthly_html)
    matches = [
        re.search(r"Google\s+AI\s+Ultra\s+" + tier + r"\s*\(\s*" + storage + r"\s*TB\s+of\s+storage\s*\)", identity, re.I)
        for tier, storage in (("5x", "20"), ("20x", "30"))
    ]
    monthly_match = re.search(r"Ultra\s+plans\s+are\s+billed\s+monthly\s+only", monthly, re.I)
    conflict = bool(
        re.search(r"Ultra\s+plans\s+are\s+billed\s+(?:annually|yearly|monthly\s+(?:or|and)\s+(?:annually|yearly))", monthly, re.I)
    )
    verified = all(matches) and bool(monthly_match) and not conflict
    return {
        "verified": verified,
        "verifiedAt": verified_at or now(),
        "identitySource": IDENTITY_URL,
        "periodSource": MONTHLY_URL,
        "mapping": {"20TB": "ultra5x", "30TB": "ultra20x"} if all(matches) else {},
        "monthlyOnly": bool(monthly_match) and not conflict,
        "identityText": " | ".join(m.group(0) for m in matches if m),
        "periodText": monthly_match.group(0) if monthly_match else None,
        "reason": "官方明确 Ultra 5x 对应 20 TB、20x 对应 30 TB，且 Ultra 仅月付" if verified else "官方产品映射或仅月付政策未核验",
    }


def verified_policy_record(value):
    if (
        not isinstance(value, dict)
        or value.get("verified") is not True
        or value.get("identitySource") != IDENTITY_URL
        or value.get("periodSource") != MONTHLY_URL
        or value.get("monthlyOnly") is not True
        or value.get("mapping") != {"20TB": "ultra5x", "30TB": "ultra20x"}
        or not policy_fresh(value)
    ):
        return None
    parsed = parse_policy(value.get("identityText", ""), value.get("periodText", ""), verified_at=value["verifiedAt"])
    return parsed if parsed.get("verified") else None


def verify_policy(emit=lambda message: None, *, force: bool = False, stop=None, policy_path: Path | None = None):
    global _POLICY
    target_path = policy_path or POLICY_PATH
    with POLICY_LOCK:
        fallback = verified_policy_record(_POLICY)
        if fallback and not force:
            return copy.deepcopy(_POLICY)
        policy_change_unverified = bool((_POLICY or {}).get("policyChangeUnverified"))
        stored_value = read_json(target_path)
        if isinstance(stored_value, dict):
            policy_change_unverified |= bool(stored_value.get("policyChangeUnverified"))
            stored = verified_policy_record(stored_value)
            if stored and (not fallback or stored["verifiedAt"] > fallback["verifiedAt"]):
                fallback = stored
        if policy_change_unverified:
            fallback = None
        if fallback and not force:
            _POLICY = fallback
            return copy.deepcopy(fallback)
        if _POLICY and not force and not _POLICY.get("verified") and policy_fresh(_POLICY, max_age=60):
            return copy.deepcopy(_POLICY)
        emit("Apple 依据：核验 Google Ultra 档位映射及仅月付政策")
        try:
            documents = []
            with requests.Session(impersonate="chrome", trust_env=False) as session:
                for url in (IDENTITY_URL, MONTHLY_URL):
                    if stop and stop.is_set():
                        raise InterruptedError()
                    response = session.get(url, timeout=12, headers={"Accept-Language": "en-US,en;q=0.9"})
                    final = urlparse(response.url)
                    source = urlparse(url)
                    if response.status_code != 200 or final.scheme != "https" or final.hostname != source.hostname:
                        raise ValueError("Official evidence unavailable")
                    if source.hostname == "support.google.com" and final.path.rstrip("/") != source.path.rstrip("/"):
                        raise ValueError("Unexpected help response")
                    if source.hostname == "one.google.com" and not final.path.rstrip("/").endswith("/about/google-ai-plans"):
                        raise ValueError("Unexpected plans response")
                    documents.append(response.text)
            policy = parse_policy(*documents)
            if not policy.get("verified"):
                policy["policyChangeUnverified"] = True
        except Exception as error:
            if fallback and policy_fresh(fallback):
                policy = copy.deepcopy(fallback)
                policy.update(
                    lastAttemptAt=now(),
                    latestAttemptStatus="error",
                    latestAttemptError=type(error).__name__,
                    reason="官方依据刷新请求未完成；沿用仍在 24 小时有效期内的已验证依据，原核验时间不变",
                )
            else:
                policy = {
                    "verified": False,
                    "verifiedAt": now(),
                    "identitySource": IDENTITY_URL,
                    "periodSource": MONTHLY_URL,
                    "mapping": {},
                    "monthlyOnly": False,
                    "reason": "Google 官方依据请求未完成（" + type(error).__name__ + "）",
                }
                if policy_change_unverified:
                    policy["policyChangeUnverified"] = True
        _POLICY = policy
        write_json(target_path, policy)
        emit(policy["reason"])
        return copy.deepcopy(policy)


def blank_cell(plan: str, status: str = "pending", reason: str = "Apple 公开列表尚无可核验的该档月付商品") -> dict:
    return {
        "status": status,
        "plan": plan,
        "amount": None,
        "currency": None,
        "period": None,
        "sourceUrl": None,
        "appId": None,
        "appName": None,
        "observedAt": now(),
        "reason": reason,
        "identityVerified": False,
        "periodVerified": False,
        "regionVerified": False,
        "candidates": [],
        "priceScope": "apple_public_monthly_iap",
        "evidence": {},
    }


def parse_amount(display: str, currency: str) -> float:
    return parse_apple_display_amount(display, currency)


def period(raw):
    if isinstance(raw, dict):
        unit = str(raw.get("unit") or raw.get("periodUnit") or "").lower()
        count = raw.get("numberOfUnits", raw.get("value", 1))
        return "month" if unit in {"month", "monthly"} and count in {1, "1"} else "year" if unit in {"year", "yearly"} else "other" if unit else None
    text = unicodedata.normalize("NFKC", str(raw or "")).lower()
    if re.search(r"\b(?:[2-9]|1[0-2])\s*months?\b|季度|半年", text):
        return "other"
    if re.search(r"\b(yearly|annually|annual|year|p1y)\b|每年|年付|年度", text):
        return "year"
    if re.search(r"\b(monthly|month|p1m)\b|每月|月付|月度", text):
        return "month"
    return None


def product_identity(name: str, policy: dict):
    name = unicodedata.normalize("NFKC", clean_text(name))
    match = re.match(r"^Google\s+(?:One\s+)?AI\s+Ultra\b", name, re.I)
    if not match:
        return None, None
    tail = name[match.end():]
    tiers = re.findall(r"(?<!\d)(5|20)\s*(?:x|倍)(?!\w)", tail, re.I)
    storage = re.findall(r"(?<!\d)(20|30)\s*TB\b", tail, re.I)
    if len(set(tiers)) > 1 or len(set(storage)) > 1:
        return None, "档位或容量冲突"
    direct = "ultra" + tiers[0] + "x" if tiers else None
    mapped = policy.get("mapping", {}).get(storage[0] + "TB") if storage else None
    if direct and mapped and direct != mapped:
        return None, "档位与官方容量映射冲突"
    if direct:
        return direct, "explicit_5x_20x_product_name"
    if mapped and policy.get("verified"):
        return mapped, "official_google_ultra_storage_mapping"
    return None, "Google Ultra 商品未区分 5 倍或 20 倍"


def text_pairs(page_html: str) -> list[dict]:
    pairs = []
    for match in re.finditer(r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue

        def walk(node):
            if isinstance(node, dict):
                if node.get("$kind") == "textPair" and isinstance(node.get("leadingText"), str) and isinstance(node.get("trailingText"), str):
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
        r'<div\b[^>]*class=["\'][^"\']*text-pair[^"\']*["\'][^>]*>\s*<span\b[^>]*>(.*?)</span>\s*<span\b[^>]*>(.*?)</span>',
        page_html,
        re.S | re.I,
    ):
        pairs.append({"name": clean_text(name), "displayPrice": clean_text(price), "sourcePeriod": None, "productId": None})
    unique = {}
    for pair in pairs:
        key = (clean_text(pair["name"]), clean_text(pair["displayPrice"]))
        if key not in unique or pair.get("sourcePeriod"):
            unique[key] = pair
    return list(unique.values())


def source_matches(url: str, country: str, app_id: str) -> bool:
    parsed = urlparse(str(url or ""))
    pieces = parsed.path.strip("/").split("/")
    return (
        parsed.scheme == "https"
        and parsed.hostname == "apps.apple.com"
        and not parsed.username
        and not parsed.password
        and bool(pieces)
        and pieces[0].upper() == country
        and bool(re.search(r"/id" + re.escape(app_id) + r"(?:/|$)", parsed.path))
    )


def apple_source_matches(url: str, country: str, app_id: str, allowed_apps=None) -> bool:
    allowed = allowed_apps if allowed_apps is not None else allowed_apple_apps()
    if not isinstance(app_id, str) or app_id not in allowed or not re.fullmatch(r"[A-Z]{2}", str(country)):
        return False
    source = urlparse(str(url or ""))
    return (
        source.scheme == "https"
        and source.netloc == "apps.apple.com"
        and bool(re.fullmatch(r"/" + country.lower() + r"/app/(?:[^/]+/)?id" + app_id + r"/?", source.path))
        and not source.fragment
    )


def apple_identity_valid(value: dict, proof: dict) -> bool:
    name = unicodedata.normalize("NFKC", str(proof.get("sourceProductName") or ""))
    if name != unicodedata.normalize("NFKC", str(value.get("sourceProductName") or "")):
        return False
    match = re.match(r"^Google\s+(?:One\s+)?AI\s+Ultra\b", name, re.I)
    if not match or re.search(
        r"\b(trial|introductory|intro|promo(?:tional)?|discount|offer|student|legacy|old|yearly|annually|annual|year)\b|试用|优惠|折扣|学生|旧版|每年|年付|年度",
        name,
        re.I,
    ):
        return False
    tail = name[match.end():]
    tiers = set(re.findall(r"(?<!\d)(5|20)\s*(?:x|倍)(?!\w)", tail, re.I))
    storage = set(re.findall(r"(?<!\d)(20|30)\s*TB\b", tail, re.I))
    expected_tier, expected_storage = ("5", "20") if value.get("plan") == "ultra5x" else ("20", "30")
    if len(tiers) > 1 or len(storage) > 1 or (tiers and tiers != {expected_tier}) or (storage and storage != {expected_storage}):
        return False
    basis = proof.get("identityBasis")
    if basis == "explicit_5x_20x_product_name":
        return bool(tiers == {expected_tier} and proof.get("identitySource") == value.get("sourceUrl"))
    text = str(proof.get("policyIdentityText") or "")
    return bool(
        basis == "official_google_ultra_storage_mapping"
        and storage == {expected_storage}
        and proof.get("identitySource") == APPLE_IDENTITY
        and re.search(r"Google\s+AI\s+Ultra\s+" + expected_tier + r"x\s*\(\s*" + expected_storage + r"\s*TB\s+of\s+storage\s*\)", text, re.I)
        and policy_at_quote(proof.get("policyVerifiedAt"), value.get("observedAt"))
    )


def valid_apple_price(value, country: str, allowed_apps=None) -> bool:
    if not isinstance(value, dict):
        return False
    amount = value.get("amount")
    source = urlparse(str(value.get("sourceUrl") or ""))
    try:
        stamp = datetime.fromisoformat(value.get("observedAt") or "")
        dated = stamp.tzinfo is not None
    except (ValueError, TypeError):
        dated = False
    basic = (
        value.get("plan") in PLANS
        and value.get("status") == "ok"
        and type(amount) in (int, float)
        and math.isfinite(amount)
        and amount > 0
        and bool(re.fullmatch(r"[A-Z]{3}", str(value.get("currency") or "")))
        and value.get("period") == "month"
        and dated
        and all(value.get(f) is True for f in ("identityVerified", "periodVerified", "regionVerified"))
        and not value.get("isPromotional")
        and not value.get("isAnnual")
        and source.scheme == "https"
        and source.hostname
        and not source.username
        and not source.password
    )
    if not basic:
        return False
    proof = value.get("evidence") or {}
    if not isinstance(proof, dict) or value.get("isGrandfathered") or proof.get("excludedAsGrandfathered"):
        return False
    direct_period = proof.get("sourcePeriod") == "month" and proof.get("periodSource") == value["sourceUrl"]
    policy_period = (
        proof.get("sourcePeriod") in (None, "month")
        and proof.get("periodSource") == APPLE_PERIOD
        and re.search(r"Ultra\s+plans\s+are\s+billed\s+monthly\s+only", str(proof.get("policyPeriodText") or ""), re.I)
        and policy_at_quote(proof.get("policyVerifiedAt"), value["observedAt"])
    )
    return bool(
        value.get("priceScope") == "apple_public_monthly_iap"
        and apple_source_matches(value["sourceUrl"], country, value.get("appId"), allowed_apps=allowed_apps)
        and apple_source_matches(proof.get("canonicalUrl"), country, value.get("appId"), allowed_apps=allowed_apps)
        and proof.get("requestCountry") == country
        and proof.get("storefrontCountry") == country
        and not proof.get("conflictingPublicOffers")
        and apple_identity_valid(value, proof)
        and (direct_period or policy_period)
    )


def parse_storefront(page_html: str, *, app_id: str, app_name: str, country: str, source_url: str, policy: dict, allowed_apps=None) -> list[dict]:
    observed = now()
    policy_ok = policy.get("verified") is True and policy_fresh(policy)
    allowed = allowed_apps if allowed_apps is not None else allowed_apple_apps()
    currencies = set(re.findall(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', page_html))
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    canonical = re.search(r'<link\b[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', page_html, re.I)
    if not canonical:
        canonical = re.search(r'<link\b[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']canonical["\']', page_html, re.I)
    canonical_url = html.unescape(canonical.group(1)) if canonical else source_url
    region_ok = source_matches(source_url, country, app_id) and source_matches(canonical_url, country, app_id)
    products = []
    for raw in text_pairs(page_html):
        name = clean_text(raw["name"])
        if not re.search(r"Ultra|\b(?:20|30)\s*TB\b", name, re.I):
            continue
        plan, basis = product_identity(name, policy if policy_ok else {})
        try:
            amount = parse_amount(raw["displayPrice"], currency) if currency else None
        except (ValueError, InvalidOperation):
            amount = None
        stated_period = period(raw.get("sourcePeriod")) or period(name)
        explicit_annual = period(name) in {"year", "other"} or period(raw.get("sourcePeriod")) in {"year", "other"}
        monthly = not explicit_annual and (stated_period == "month" or (policy_ok and policy.get("monthlyOnly")))
        promotional = bool(re.search(r"\b(trial|introductory|intro|promo(?:tional)?|discount|offer|student|legacy|old)\b|试用|优惠|折扣|学生|旧版", name, re.I))
        accepted = bool(plan and monthly and amount and currency and region_ok and not promotional and app_id in allowed)
        reason = (
            "已核验当前 Ultra 档位与月付政策"
            if accepted
            else (
                "跨区 Apple 来源，不能作为当地价格"
                if not region_ok
                else "优惠、试用或明确旧版商品，不进入标准月价"
                if promotional
                else "年付或非单月商品，不进入月价"
                if explicit_annual
                else "商品身份待核实，不按裸容量或金额猜档位"
                if not plan
                else "月付周期尚未核验"
                if not monthly
                else "金额或币种尚未核验"
            )
        )
        evidence = {
            "requestCountry": country,
            "storefrontCountry": country if region_ok else None,
            "responseStorefront": urlparse(source_url).path.strip("/").split("/", 1)[0].upper(),
            "developerId": GOOGLE_DEVELOPER_ID,
            "identityBasis": basis,
            "identitySource": IDENTITY_URL if basis == "official_google_ultra_storage_mapping" else source_url,
            "identitySourceUrl": IDENTITY_URL,
            "periodSource": source_url if stated_period == "month" else MONTHLY_URL,
            "periodSourceUrl": source_url if stated_period == "month" else MONTHLY_URL,
            "policyVerifiedAt": policy.get("verifiedAt"),
            "policyIdentityText": policy.get("identityText"),
            "policyPeriodText": policy.get("periodText"),
            "sourceProductName": name,
            "productId": raw.get("productId"),
            "skuPubliclyExposed": bool(raw.get("productId")),
            "canonicalUrl": canonical_url,
            "sourcePeriod": stated_period,
            "verificationScope": "public_apple_offer_and_official_google_policy",
            "checkoutVerified": False,
        }
        products.append({
            "status": "ok" if accepted else "pending",
            "plan": plan,
            "amount": amount if accepted else None,
            "currency": currency if accepted else None,
            "sourceAmount": amount,
            "sourceCurrency": currency,
            "period": "month" if monthly else None,
            "sourceUrl": source_url,
            "appId": app_id,
            "appName": app_name,
            "countryCode": country,
            "observedAt": observed,
            "sourceProductName": name,
            "displayPrice": clean_text(raw["displayPrice"]),
            "productId": raw.get("productId"),
            "identityVerified": bool(plan),
            "periodVerified": monthly,
            "regionVerified": region_ok,
            "isPromotional": promotional,
            "reason": reason,
            "priceScope": "apple_public_monthly_iap",
            "evidence": evidence,
        })
    for plan in PLANS:
        eligible = [item for item in products if item["plan"] == plan and item["status"] == "ok"]
        if len({(item["currency"], item["amount"]) for item in eligible}) <= 1:
            continue
        if len({item["currency"] for item in eligible}) > 1:
            for item in eligible:
                item.update(status="pending", amount=None, currency=None, reason="同一 App 同档位出现多个不同币种，无法区分，已拦截不计入")
                item["evidence"]["conflictingPublicOffers"] = True
            continue
        amounts = sorted({item["amount"] for item in eligible})
        min_amount, max_amount = amounts[0], amounts[-1]
        if max_amount / min_amount >= 8.5 and not any(item["evidence"].get("sourcePeriod") == "month" for item in eligible if item["amount"] == max_amount):
            for item in eligible:
                item.update(status="pending", amount=None, currency=None, reason="同一 App 同档位金额跨度过大（疑似含未标年付商品），已拦截不计入")
                item["evidence"]["conflictingPublicOffers"] = True
            continue
        superseded = [a for a in amounts if a < max_amount]
        for item in eligible:
            if item["amount"] == max_amount:
                item["reason"] = "同一 App 存在涨价前后多档价格；按苹果机制取新开用户最高月价，已排除老用户旧续订价"
                item["evidence"]["priceSelection"] = "highest_new_subscriber_price"
                item["evidence"]["supersededGrandfatheredPrices"] = superseded
            else:
                item.update(
                    status="pending",
                    amount=None,
                    currency=None,
                    isGrandfathered=True,
                    reason=f"苹果涨价前老用户旧续订价（{item['displayPrice']}），新开用户需按最高新价购买，已排除",
                )
                item["evidence"]["excludedAsGrandfathered"] = True
                item["evidence"]["supersededByAmount"] = max_amount
    return products


def valid_monthly(value, allowed_apps=None) -> bool:
    if not isinstance(value, dict):
        return False
    evidence = value.get("evidence") or {}
    return isinstance(evidence, dict) and valid_apple_price(value, evidence.get("requestCountry"), allowed_apps=allowed_apps)


def select_candidates(candidates: list[dict], plan: str, *, app_count=None, allowed_apps=None) -> dict:
    own = [copy.deepcopy(p) for p in candidates if p.get("plan") == plan]
    usable = [p for p in own if valid_monthly(p, allowed_apps=allowed_apps)]
    if not usable:
        result = blank_cell(plan)
        result["candidates"] = own
        if own:
            result["reason"] = "；".join(dict.fromkeys(p["reason"] for p in own))
        return result
    if len({p["currency"] for p in usable}) != 1:
        result = blank_cell(plan, reason="同地区多个 Apple 入口币种不一致，待核实后比较")
        result["candidates"] = own
        return result
    amounts = sorted({p["amount"] for p in usable})
    if len(amounts) > 1:
        min_amount, max_amount = amounts[0], amounts[-1]
        if max_amount / min_amount >= 8.5 and not any(
            p.get("evidence", {}).get("sourcePeriod") == "month" for p in usable if p["amount"] == max_amount
        ):
            for p in own:
                if valid_monthly(p, allowed_apps=allowed_apps):
                    p.update(status="pending", amount=None, currency=None, reason="同地区多入口出现疑似未标注年付的大跨度差价，待核实后比较")
                    p.setdefault("evidence", {})["conflictingPublicOffers"] = True
            result = blank_cell(plan, reason="同地区多入口出现疑似未标注年付的大跨度差价，待核实后比较")
            result["candidates"] = own
            return result
    result = copy.deepcopy(
        max(
            usable,
            key=lambda p: (
                p["amount"],
                not bool(p.get("retainedVerified")),
                -(int(p["appId"]) if str(p.get("appId", "")).isdigit() else 0),
            ),
        )
    )
    superseded = sorted({p["amount"] for p in usable if p["amount"] < result["amount"]})
    existing_superseded = result.get("evidence", {}).get("supersededGrandfatheredPrices") or []
    all_superseded = sorted(set(superseded) | set(existing_superseded))
    if all_superseded:
        result.setdefault("evidence", {})["priceSelection"] = "highest_new_subscriber_price"
        result["evidence"]["supersededGrandfatheredPrices"] = all_superseded
        for p in own:
            if p.get("status") == "ok" and p.get("amount") is not None and p["amount"] < result["amount"]:
                p["excludedAsGrandfathered"] = True
                p.setdefault("evidence", {})["excludedAsGrandfathered"] = True
                p["evidence"]["supersededByAmount"] = result["amount"]
                p["reason"] = "苹果涨价前老用户旧续订价，新开用户已采用更高新价"
    total_apps = app_count or len(APPS)
    result["candidates"] = own
    result["reason"] = (
        f"{total_apps} 个 Google 官方软件中按苹果涨价机制取新开用户最高有效月价（已排除旧续订价）；公开列表不保证完整"
        if all_superseded
        else f"{total_apps} 个 Google 官方软件中已核验的有效月价；公开列表不保证完整"
    )
    return result


def collect_app(country: str, app_id: str, app_name: str, *, stop=None, emit=lambda message: None, timeout: int = 12, policy=None, session=None, allowed_apps=None, limiter=None) -> dict:
    url = f"https://apps.apple.com/{country.lower()}/app/id{app_id}"
    base = {"appId": app_id, "appName": app_name, "countryCode": country, "sourceUrl": url, "observedAt": now(), "products": [], "attempts": 0}
    attempt = 1
    max_attempts = 2
    last_retry_after = None
    while attempt <= max_attempts:
        if stop and stop.is_set():
            return {**base, "status": "error", "reason": "任务已取消", "cancelled": True}
        emit(f"Apple {country} {app_name}：第 {attempt}/{max_attempts} 次请求")
        try:
            req_headers = {"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache"}
            req_timeout = min(12, max(1, timeout))
            slot_ctx = limiter.slot(stop=stop) if limiter is not None else nullcontext(True)
            with slot_ctx as acquired:
                if not acquired:
                    return {**base, "status": "error", "reason": "任务已取消", "cancelled": True}
                if session is not None:
                    response = session.get(url, timeout=req_timeout, allow_redirects=True, headers=req_headers)
                else:
                    with requests.Session(impersonate="chrome", trust_env=False) as current_session:
                        response = current_session.get(url, timeout=req_timeout, allow_redirects=True, headers=req_headers)
            base.update(attempts=attempt, httpStatus=response.status_code, sourceUrl=response.url, observedAt=now())
            base["finalStorefront"] = urlparse(response.url).path.strip("/").split("/", 1)[0].upper()
            if not source_matches(response.url, country, app_id):
                if limiter is not None:
                    limiter.note_success()
                return {**base, "status": "pending", "reason": "Apple 跨区重定向；不能据此判定当地无订阅"}
            if response.status_code == 404:
                if limiter is not None:
                    limiter.note_success()
                return {**base, "status": "unavailable", "availabilityConfirmed": True, "reason": "Apple 确认该地区未上架此 App（HTTP 404）"}
            if response.status_code in {429, 503}:
                raw_ra = getattr(response, "headers", {}).get("Retry-After") if hasattr(getattr(response, "headers", None), "get") else None
                last_retry_after = None
                if isinstance(raw_ra, (int, float, str)):
                    try:
                        last_retry_after = max(0.0, min(15.0, float(raw_ra)))
                    except ValueError:
                        last_retry_after = None
                if limiter is not None:
                    limiter.note_rate_limit(last_retry_after)
                max_attempts = 4
                raise ValueError(f"HTTP {response.status_code}")
            if response.status_code != 200:
                raise ValueError("HTTP request failed")
            if limiter is not None:
                limiter.note_success()
            products = parse_storefront(
                response.text,
                app_id=app_id,
                app_name=app_name,
                country=country,
                source_url=response.url,
                policy=policy or {},
                allowed_apps=allowed_apps,
            )
            base["products"] = products
            count = len({p["plan"] for p in products if valid_monthly(p, allowed_apps=allowed_apps)})
            return {
                **base,
                "status": "ok" if count == 2 else "partial" if count else "pending",
                "reason": "已取得可核验月价" if count else "公开列表未列出可核验的两档 Ultra 月价；不等于未上架",
            }
        except Exception as error:
            base["errorType"] = type(error).__name__
            emit(f"Apple {country} {app_name}：请求未完成（{type(error).__name__}）")
            if attempt < max_attempts:
                delay = (0.5 if limiter is not None else max(last_retry_after or 0.0, min(15.0, attempt * 2.5))) if base.get("httpStatus") in {429, 503} else 0.4
                if stop:
                    stop.wait(delay)
                else:
                    time.sleep(delay)
        attempt += 1
    return {**base, "status": "error", "reason": "Apple 请求失败" + (f"（HTTP {base['httpStatus']}）" if base.get("httpStatus") else "")}


def fetch_country(code: str, stop=None, emit=lambda message: None, *, timeout: int = 12, policy=None, catalog=None, limiter=None) -> dict:
    code = str(code).upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError("Invalid country")
    policy = verify_policy(emit, stop=stop) if policy is None else policy
    if isinstance(catalog, dict):
        raw_map = catalog.get("iapApps") if isinstance(catalog.get("iapApps"), dict) and catalog.get("iapApps") else catalog.get("apps")
        apps_map = order_app_map(raw_map) if isinstance(raw_map, dict) and raw_map else dict(APPS)
        total_developer_apps = len(catalog["apps"]) if isinstance(catalog.get("apps"), dict) and catalog.get("apps") else len(apps_map)
        no_iap_count = len(catalog["noIapApps"]) if isinstance(catalog.get("noIapApps"), dict) else 0
    else:
        apps_map = dict(APPS)
        total_developer_apps = len(apps_map)
        no_iap_count = 0
    allowed = set(allowed_apple_apps()) | set(apps_map)
    results_by_id = {}
    items = list(apps_map.items())
    country_session = requests.Session(impersonate="chrome", trust_env=False) if (len(items) > 8 or limiter is not None) else None
    try:
        if len(items) > 8:
            probe_items, rest_items = items[:3], items[3:]
            for app_id, name in probe_items:
                if stop and stop.is_set():
                    break
                results_by_id[app_id] = collect_app(
                    code, app_id, name, stop=stop, emit=emit, timeout=timeout, policy=policy, session=country_session, allowed_apps=allowed, limiter=limiter
                )
            probe_results = [results_by_id.get(aid) for aid, _ in probe_items if aid in results_by_id]
            redirected_targets = {r.get("finalStorefront") for r in probe_results if r and r.get("finalStorefront")}
            all_redirected = (
                not (stop and stop.is_set())
                and len(probe_results) == len(probe_items)
                and all(r.get("finalStorefront") and r.get("finalStorefront") != code and not r.get("cancelled") for r in probe_results)
                and not any(valid_monthly(p, allowed_apps=allowed) for r in probe_results for p in r.get("products", []))
            )
            if all_redirected:
                target = next(iter(sorted(redirected_targets)))
                emit(f"Apple {code}：核心入口均跨区重定向至 {target}，跳过其余 {len(rest_items)} 个入口重复请求")
                checked_at = now()
                for app_id, name in rest_items:
                    results_by_id[app_id] = {
                        "appId": app_id,
                        "appName": name,
                        "countryCode": code,
                        "sourceUrl": f"https://apps.apple.com/{code.lower()}/app/id{app_id}",
                        "observedAt": checked_at,
                        "products": [],
                        "attempts": 0,
                        "status": "pending",
                        "finalStorefront": target,
                        "skippedStorefrontRedirect": True,
                        "reason": f"Apple 前 3 个核心入口均跨区重定向至 {target}，跳过其余入口重复请求",
                    }
            else:
                for app_id, name in rest_items:
                    if stop and stop.is_set():
                        break
                    results_by_id[app_id] = collect_app(
                        code, app_id, name, stop=stop, emit=emit, timeout=timeout, policy=policy, session=country_session, allowed_apps=allowed, limiter=limiter
                    )
        else:
            for app_id, name in items:
                if stop and stop.is_set():
                    break
                results_by_id[app_id] = collect_app(
                    code, app_id, name, stop=stop, emit=emit, timeout=timeout, policy=policy, session=country_session, allowed_apps=allowed, limiter=limiter
                )
    finally:
        if country_session is not None:
            try:
                country_session.close()
            except Exception:
                pass

    apps = [results_by_id[app_id] for app_id, _ in items if app_id in results_by_id]
    candidates = [p for app in apps for p in app.get("products", [])]
    cells = {plan: select_candidates(candidates, plan, app_count=total_developer_apps, allowed_apps=allowed) for plan in PLANS}
    count = sum(valid_monthly(c, allowed_apps=allowed) for c in cells.values())
    complete = len(apps) == len(apps_map)
    absent = complete and all(a.get("status") == "unavailable" and a.get("availabilityConfirmed") for a in apps)
    failed = complete and all(a.get("status") == "error" for a in apps)
    if absent or failed:
        cells = {
            p: blank_cell(
                p,
                "unavailable" if absent else "error",
                f"{len(apps_map)} 个 Apple 入口均确认未上架" if absent else f"{len(apps_map)} 个 Apple 入口请求均失败",
            )
            for p in PLANS
        }
    return {
        "countryCode": code,
        "status": "ok" if count == 2 else "partial" if count else "unavailable" if absent else "error" if failed else "pending",
        "reason": f"已检查 {len(apps)}/{len(apps_map)} 个含内购入口（共 {total_developer_apps} 个 Google 官方软件）；按新开用户取最高有效月价",
        "observedAt": now(),
        "availabilityConfirmed": absent,
        "apps": apps,
        "cells": cells,
        "entryCount": len(apps_map),
        "developerAppCount": total_developer_apps,
        "noIapAppCount": no_iap_count,
        "developerId": GOOGLE_DEVELOPER_ID,
        "cancelled": bool(stop and stop.is_set()),
        "policy": copy.deepcopy(policy),
    }
