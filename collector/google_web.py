"""Google AI Ultra localized monthly list-price collector via matched residential IP."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from collector.common import DATA_DIR, now_iso, read_json, write_json
from collector import proxy_net

PAGE_URL = "https://one.google.com/about/google-ai-plans/"
WEB_POLICY = PAGE_URL
PLANS = ("ultra5x", "ultra20x")
PRICE_FIELDS = {"ultra5x": "PRICE_GEN_AI_ULTRA_100_MONTHLY", "ultra20x": "PRICE_GEN_AI_ULTRA_200_MONTHLY"}
POLICY_PATH = DATA_DIR / "web-price-policy.json"
_POLICY = None
_POLICY_LOCK = threading.Lock()
_TEMPLATE_COUNTRY_CACHE: dict[str, str] = {}
_TEMPLATE_LOCK = threading.Lock()


def now() -> str:
    return now_iso()


def _cell(plan: str, status: str = "pending", reason: str = "尚未采集") -> dict:
    return {
        "plan": plan,
        "status": status,
        "amount": None,
        "currency": None,
        "period": None,
        "sourceUrl": None,
        "observedAt": None,
        "identityVerified": False,
        "periodVerified": False,
        "regionVerified": False,
        "reason": reason,
        "candidates": [],
    }


def _channel(status: str = "pending", reason: str = "尚未采集") -> dict:
    return {"status": status, "reason": reason, "cells": {p: _cell(p, status, reason) for p in PLANS}}


def _official_page(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "one.google.com" and not parsed.username and not parsed.password


def parse_identity_policy(page_html: str) -> dict:
    soup = BeautifulSoup(page_html, "html.parser")
    mapping = {}
    for plan, key in PRICE_FIELDS.items():
        multiplier = "5" if plan == "ultra5x" else "20"
        candidates = []
        for heading in soup.select("th"):
            text = heading.get_text(" ", strip=True).replace("\u00a0", " ")
            if re.search(r"Google\s+AI\s+Ultra\s*" + multiplier + r"\s*[x×]\b", text, re.I):
                widgets = heading.select("g1-localized-price")
                if (
                    len(widgets) == 1
                    and widgets[0].get("variant") == key
                    and re.search(r"/\s*mo\b|per\s+month|monthly", text, re.I)
                    and not re.search(r"annual|yearly|promo|trial|first\s+\d+", text, re.I)
                ):
                    candidates.append(key)
        if candidates and len(set(candidates)) == 1:
            mapping[plan] = key
    return mapping


def parse_feed_version(script: str) -> str | None:
    if "g1-localized-price" not in script or "COUNTRY_CODE" not in script or "CURRENCY_CODE" not in script:
        return None
    matches = re.findall(r"\w+\(\)\s*\|\|\s*[`\"'](pricing_\d{4}_\d{2}_\d{2}\.json)[`\"']", script)
    return matches[0] if len(set(matches)) == 1 else None


def policy_valid(policy) -> bool:
    if not isinstance(policy, dict) or policy.get("verified") is not True or policy.get("mapping") != PRICE_FIELDS:
        return False
    if (
        policy.get("sourceUrl") != PAGE_URL
        or not _official_page(str(policy.get("componentUrl") or ""))
        or not re.fullmatch(r"pricing_\d{4}_\d{2}_\d{2}\.json", str(policy.get("feedVersion") or ""))
    ):
        return False
    try:
        checked = datetime.fromisoformat(policy["observedAt"])
        return checked.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - checked).total_seconds() < 86400
    except (KeyError, ValueError, TypeError):
        return False


def policy_at_quote(policy_date, quote_date) -> bool:
    try:
        policy = datetime.fromisoformat(policy_date or "")
        quote = datetime.fromisoformat(quote_date or "")
        return bool(policy.tzinfo and quote.tzinfo and 0 <= (quote - policy).total_seconds() <= 86400)
    except (TypeError, ValueError):
        return False


def verify_policy(emit=lambda message: None, force: bool = False, stop=None, policy_path: Path | None = None):
    global _POLICY
    target_path = policy_path or POLICY_PATH
    with _POLICY_LOCK:
        cached = _POLICY or read_json(target_path)
        if not force and policy_valid(cached):
            _POLICY = cached
            return copy.deepcopy(cached)
        if stop and stop.is_set():
            return {"verified": False, "reason": "cancelled"}
        emit("官网依据：核验 Google 官方 5 倍／20 倍月付字段及当前地区价格源")
        try:
            with requests.Session(impersonate="chrome", trust_env=False) as session:
                response = session.get(PAGE_URL, timeout=12, headers={"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache"})
                if response.status_code != 200 or response.url.rstrip("/") != PAGE_URL.rstrip("/"):
                    raise ValueError("Official comparison page unavailable")
                mapping = parse_identity_policy(response.text)
                if mapping != PRICE_FIELDS:
                    raise ValueError("Explicit tier monthly binding unavailable")
                soup = BeautifulSoup(response.text, "html.parser")
                urls = []
                for node in soup.select("script[src]"):
                    url = urljoin(PAGE_URL, node.get("src"))
                    if _official_page(url) and urlparse(url).path.startswith("/about/assets/"):
                        urls.append(url)
                if len(urls) > 40:
                    raise ValueError("Unexpected component collection")

            def component(url):
                if stop and stop.is_set():
                    return None
                try:
                    with requests.Session(impersonate="chrome", trust_env=False) as current:
                        result = current.get(url, timeout=8)
                    if result.status_code == 200 and result.url == url:
                        version = parse_feed_version(result.text)
                        if version:
                            return url, version
                except Exception:
                    return None
                return None

            with ThreadPoolExecutor(max_workers=6) as pool:
                matches = [item for item in pool.map(component, urls) if item]
            if len(matches) != 1:
                raise ValueError("Production price feed ambiguous")
            policy = {
                "verified": True,
                "mapping": mapping,
                "sourceUrl": PAGE_URL,
                "componentUrl": matches[0][0],
                "feedVersion": matches[0][1],
                "observedAt": now(),
                "reason": "官方比较表逐列确认 5x／20x 对应独立 MONTHLY 字段；生产组件确认地区价格源",
            }
            if stop and stop.is_set():
                return {"verified": False, "reason": "cancelled"}
            write_json(target_path, policy)
            _POLICY = policy
            emit("官网依据通过：Google AI Ultra 5x、20x 均为独立月付字段")
            return copy.deepcopy(policy)
        except Exception as error:
            policy = {
                "verified": False,
                "sourceUrl": PAGE_URL,
                "observedAt": now(),
                "reason": "官方套餐／月付来源暂无法核实（" + type(error).__name__ + "）",
            }
            emit(policy["reason"])
            _POLICY = policy
            return policy


def feed_url(country: str, policy: dict) -> str:
    if not re.fullmatch(r"[A-Z]{2}", country) or not policy_valid(policy):
        raise ValueError("Unverified country/feed")
    slug = "uk" if country == "GB" else country.lower()
    return f"https://one.google.com/intl/ALL_{slug}/about/feeds/{policy['feedVersion']}"


def localized_plans_url(country: str) -> str:
    if not re.fullmatch(r"[A-Z]{2}", str(country)):
        raise ValueError("Invalid country code")
    slug = "uk" if country == "GB" else country.lower()
    return f"https://one.google.com/intl/ALL_{slug}/about/google-ai-plans/"


def parse_feed_amount(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,3})?", text):
            return None
        amount = float(text.replace(",", ""))
    else:
        return None
    return amount if math.isfinite(amount) and amount > 0 else None


def parse_google_ip_country(page_html: str, final_url=None) -> str | None:
    if isinstance(page_html, str) and page_html:
        soup = BeautifulSoup(page_html, "html.parser")
        codes = set()
        for node in soup.select("g1-localized-price[country]"):
            raw = str(node.get("country") or "").strip().upper()
            code = "GB" if raw == "UK" else raw
            if code and code != "ZZ" and re.fullmatch(r"[A-Z]{2}", code):
                codes.add(code)
        if len(codes) == 1:
            return next(iter(codes))
    if isinstance(final_url, str) and final_url:
        match = re.search(r"/intl/ALL_([a-z]{2})(?:/|$)", urlparse(final_url).path, re.I)
        if match:
            code = match.group(1).upper()
            return "GB" if code == "UK" else code
    return None


def check_google_template_country(country: str, *, stop=None) -> str | None:
    if not re.fullmatch(r"[A-Z]{2}", str(country)):
        return None
    with _TEMPLATE_LOCK:
        if country in _TEMPLATE_COUNTRY_CACHE:
            return _TEMPLATE_COUNTRY_CACHE[country]
    if stop and stop.is_set():
        return None
    try:
        with requests.Session(impersonate="chrome", trust_env=False) as session:
            resp = session.get(
                localized_plans_url(country),
                timeout=8,
                allow_redirects=True,
                headers={"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache"},
            )
        if resp.status_code != 200:
            return None
        detected = parse_google_ip_country(resp.text if isinstance(getattr(resp, "text", None), str) else "", final_url=None)
        if detected:
            with _TEMPLATE_LOCK:
                _TEMPLATE_COUNTRY_CACHE[country] = detected
        return detected
    except Exception:
        return None


def valid_web_price(value, country: str) -> bool:
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
    if not isinstance(proof, dict) or proof.get("currencyAmountConflict"):
        return False
    component = urlparse(str(proof.get("componentUrl") or ""))
    version = proof.get("feedVersion")
    slug = "uk" if country == "GB" else str(country).lower()
    expected = PRICE_FIELDS[value["plan"]]
    return (
        bool(re.fullmatch(r"[A-Z]{2}", str(country)))
        and source.netloc == "one.google.com"
        and not source.query
        and not source.fragment
        and bool(re.fullmatch(r"pricing_\d{4}_\d{2}_\d{2}\.json", str(version)))
        and source.path == f"/intl/ALL_{slug}/about/feeds/{version}"
        and value.get("priceScope") == "official_regional_list_price"
        and proof.get("requestCountry") == country
        and proof.get("quoteCountry") == country
        and proof.get("verifiedProxyCountry") == country
        and proof.get("proxyVerification") == "same_sticky_session"
        and proof.get("transport") == "residential_proxy"
        and proof.get("productId") == expected
        and proof.get("priceField") == expected
        and type(proof.get("intervalCount")) is int
        and proof.get("intervalCount") == 1
        and proof.get("intervalUnit") == "MONTH"
        and all(proof.get(key) == WEB_POLICY for key in ("policySource", "identitySource", "periodSource"))
        and component.scheme == "https"
        and component.netloc == "one.google.com"
        and bool(re.fullmatch(r"/about/assets/[A-Za-z0-9_./-]+\.js", component.path))
        and not component.query
        and not component.fragment
        and policy_at_quote(proof.get("policyObservedAt"), value.get("observedAt"))
    )


def _conflict_cell(plan: str, observed_at: str, source_url: str, reason: str, *, raw_amount, currency: str, pro_amount=None) -> dict:
    value = _cell(plan, "pending", reason)
    value.update(
        observedAt=observed_at,
        sourceUrl=source_url,
        evidence={
            "currencyAmountConflict": True,
            "rawAmount": raw_amount,
            "declaredCurrency": currency,
            **({"referenceProAmount": pro_amount} if pro_amount is not None else {}),
        },
    )
    return value


def parse_feed(payload, country: str, source_url: str, policy: dict, proxy: dict, observed_at=None, google_ip_country=None, template_fallback: bool = False) -> dict:
    result = _channel("pending", "未取得该地区可核验的月付标价")
    observed_at = observed_at or now()
    result.update(observedAt=observed_at, sourceUrl=source_url, priceScope="official_regional_list_price")
    if not policy_valid(policy) or not isinstance(payload, dict):
        result["reason"] = "官方套餐／月付依据不完整"
        return result
    if source_url != feed_url(country, policy):
        result["reason"] = "地区价格源地址不匹配；不接受重定向报价"
        return result
    quote_country = payload.get("COUNTRY_CODE")
    quote_country = "GB" if quote_country == "UK" else quote_country
    currency = payload.get("CURRENCY_CODE")
    if (
        quote_country != country
        or proxy.get("status") != "ok"
        or proxy.get("actualCountry") != country
        or not isinstance(currency, str)
        or not re.fullmatch(r"[A-Z]{3}", currency)
    ):
        result["reason"] = "报价地区、代理出口或币种未匹配；不复制通用美元价格"
        for plan in PLANS:
            result["cells"][plan]["reason"] = result["reason"]
        return result

    parsed_amounts = {plan: parse_feed_amount(payload.get(field)) for plan, field in PRICE_FIELDS.items()}
    pro_amount = parse_feed_amount(payload.get("PRICE_GEN_AI_PRO_MONTHLY"))
    a5, a20 = parsed_amounts["ultra5x"], parsed_amounts["ultra20x"]
    conflict_reasons = {}

    if a5 is not None and a20 is not None and a5 >= a20:
        pro_ok_5x = pro_amount is not None and (1.5 <= a5 / pro_amount <= 20.0)
        pro_ok_20x = pro_amount is not None and (2.0 <= a20 / pro_amount <= 30.0)
        if pro_ok_20x and not pro_ok_5x:
            conflict_reasons["ultra5x"] = f"官网价格源存在币种与金额冲突：5× 标为 {currency} {a5:g}（高于 20× {currency} {a20:g}），已拦截不计入"
        elif pro_ok_5x and not pro_ok_20x:
            conflict_reasons["ultra20x"] = f"官网价格源存在币种与金额冲突：20× 标为 {currency} {a20:g}（低于 5× {currency} {a5:g}），已拦截不计入"
        else:
            conflict_reasons["ultra5x"] = f"官网价格源 5×（{currency} {a5:g}）不低于 20×（{currency} {a20:g}），已拦截不计入"
            conflict_reasons["ultra20x"] = f"官网价格源 20×（{currency} {a20:g}）不高于 5×（{currency} {a5:g}），已拦截不计入"

    if pro_amount is not None:
        if "ultra5x" not in conflict_reasons and a5 is not None and (a5 <= pro_amount or a5 / pro_amount > 50):
            conflict_reasons["ultra5x"] = f"官网 5× 标价（{currency} {a5:g}）与 Pro（{currency} {pro_amount:g}）比例异常，疑似币种错误，已拦截"
        if "ultra20x" not in conflict_reasons and a20 is not None and (a20 <= pro_amount or a20 / pro_amount > 60):
            conflict_reasons["ultra20x"] = f"官网 20× 标价（{currency} {a20:g}）与 Pro（{currency} {pro_amount:g}）比例异常，疑似币种错误，已拦截"

    ip_verification = (
        "google_page_us_template_fallback"
        if template_fallback
        else "google_page_and_feed_matched"
        if google_ip_country == country
        else "proxy_geo_and_country_feed_matched"
    )
    currency_mode = "usd_regional_billing" if (currency == "USD" and country != "US") else "local_currency"

    for plan, field in PRICE_FIELDS.items():
        amount = parsed_amounts[plan]
        if plan in conflict_reasons:
            result["cells"][plan] = _conflict_cell(plan, observed_at, source_url, conflict_reasons[plan], raw_amount=amount, currency=currency, pro_amount=pro_amount)
            continue
        value = _cell(plan, "pending", "该地区官方价格源未返回此档正常月付标价")
        value.update(observedAt=observed_at, sourceUrl=source_url)
        if amount is not None:
            evidence = {
                "requestCountry": country,
                "quoteCountry": country,
                "verifiedProxyCountry": country,
                "proxyVerification": "same_sticky_session",
                "transport": "residential_proxy",
                "productId": field,
                "priceField": field,
                "intervalCount": 1,
                "intervalUnit": "MONTH",
                "feedVersion": policy["feedVersion"],
                "identitySource": PAGE_URL,
                "periodSource": PAGE_URL,
                "policySource": PAGE_URL,
                "policyObservedAt": policy["observedAt"],
                "componentUrl": policy["componentUrl"],
                "ipCurrencyVerification": ip_verification,
                "currencyMode": currency_mode,
            }
            if google_ip_country:
                evidence["googleIpCountry"] = google_ip_country
            value.update(
                status="ok",
                amount=float(amount),
                currency=currency,
                period="month",
                identityVerified=True,
                periodVerified=True,
                regionVerified=True,
                reason="Google 官方地区月付标价（住宅代理同会话核验）；非账号结账总额",
                priceScope="official_regional_list_price",
                evidence=evidence,
            )
        result["cells"][plan] = value
    result["status"] = "ok" if all(c["status"] == "ok" for c in result["cells"].values()) else "partial" if any(c["status"] == "ok" for c in result["cells"].values()) else "pending"
    if conflict_reasons:
        result["reason"] = "部分档位币种与金额存在官网源冲突，已拦截" if result["status"] == "partial" else "官网价格源币种与金额冲突，已拦截"
    else:
        result["reason"] = "官方地区月付标价已核验" if result["status"] == "ok" else "部分月付字段缺失，缺失不代表该地区不可购买"
    return result


def fetch_country(country: str, config: dict, transport, stop=None, emit=lambda message: None, *, timeout: int = 12, retries: int = 4, policy=None) -> dict:
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Invalid country code")
    policy = policy or verify_policy(emit, stop=stop)
    if not policy_valid(policy):
        result = _channel("pending", policy.get("reason", "官方月付依据待核实"))
        result["proxy"] = {"status": "pending", "reason": "官方来源尚未确认，未消耗逐国代理"}
        return result
    if transport is None:
        result = _channel("error", "住宅代理入口未通过预检")
        result["proxy"] = {"status": "error", "reason": "gateway_unavailable"}
        return result
    source_url = feed_url(country, policy)
    max_attempts = max(1, min(4, retries))
    last = _channel("error", "官网请求失败")
    for attempt in range(1, max_attempts + 1):
        if stop and stop.is_set():
            result = _channel("cancelled", "已取消")
            result["proxy"] = {"status": "pending", "reason": "cancelled"}
            return result
        emit(f"官网 {country}：第 {attempt}/{max_attempts} 次，核验住宅出口、IP 定位与当地月付标价")
        proxy = {"status": "error", "actualCountry": None, "reason": "request_failed", "attempts": attempt}
        try:
            kwargs = proxy_net.proxy_kwargs(transport, config, country, proxy_net.new_session_id())
            with proxy_net.create_session(transport) as session:
                proxy.update(proxy_net.verify_geo(session, kwargs, country, timeout=min(12, timeout)))
                proxy["observedAt"] = now()
                if proxy["status"] != "ok":
                    last = _channel("error", "住宅代理出口不匹配" if proxy["status"] == "mismatch" else "住宅代理无法连通")
                    last["proxy"] = proxy
                    emit(f"官网 {country}：代理 {proxy['reason']}")
                    if proxy.get("reason") == "proxy_auth_required":
                        return last
                    if attempt < max_attempts:
                        time.sleep(0.5)
                    continue
                if stop and stop.is_set():
                    last = _channel("cancelled", "已取消")
                    last["proxy"] = proxy
                    return last
                page_resp = session.get(PAGE_URL, timeout=min(12, timeout), allow_redirects=True, headers={"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache"}, **kwargs)
                if page_resp.status_code == 407:
                    last = _channel("error", "住宅代理认证失败")
                    proxy.update(status="error", reason="proxy_auth_required", httpStatus=407)
                    last["proxy"] = proxy
                    return last
                google_ip_country = (
                    parse_google_ip_country(
                        page_resp.text if isinstance(getattr(page_resp, "text", None), str) else "",
                        str(getattr(page_resp, "url", "") or ""),
                    )
                    if page_resp.status_code == 200
                    else None
                )
                if google_ip_country:
                    proxy["googleIpCountry"] = google_ip_country
                response = session.get(source_url, timeout=min(12, timeout), allow_redirects=False, headers={"Accept": "application/json", "Cache-Control": "no-cache"}, **kwargs)
                if response.status_code == 200:
                    payload = response.json()
                    quote_country = payload.get("COUNTRY_CODE") if isinstance(payload, dict) else None
                    quote_country = "GB" if quote_country == "UK" else quote_country
                    template_fallback = False
                    if (
                        quote_country == country
                        and google_ip_country
                        and re.fullmatch(r"[A-Z]{2}", google_ip_country)
                        and google_ip_country != country
                    ):
                        template_country = check_google_template_country(country, stop=stop)
                        if template_country == google_ip_country:
                            template_fallback = True
                            proxy["googleTemplateFallback"] = template_country
                        else:
                            proxy_mismatch = copy.deepcopy(proxy)
                            proxy_mismatch["reason"] = f"google_ip_mismatch:{google_ip_country}"
                            last = _channel("error", f"住宅代理 GeoIP 为 {country}，但 Google 页面识别为 {google_ip_country}，未采纳报价")
                            last.update(proxy=proxy_mismatch, sourceUrl=source_url, attempts=attempt)
                            emit(f"官网 {country}：Google 页面识别 IP 为 {google_ip_country}，更换住宅会话重试（第 {attempt}/{max_attempts} 次）")
                            if attempt < max_attempts:
                                time.sleep(0.5)
                                continue
                            return last
                    result = parse_feed(
                        payload,
                        country,
                        response.url,
                        policy,
                        proxy,
                        google_ip_country=google_ip_country,
                        template_fallback=template_fallback,
                    )
                    result["proxy"] = proxy
                    result["attempts"] = attempt
                    return result
                reason = (
                    "官网地区源重定向，未采用其他地区报价"
                    if response.status_code in {301, 302, 303, 307, 308}
                    else "官网未公开该地区价格源；不能据此判为不支持"
                    if response.status_code == 404
                    else f"官网价格请求 HTTP {response.status_code}"
                )
                last = _channel("pending" if response.status_code in {301, 302, 303, 307, 308, 404} else "error", reason)
                last.update(proxy=proxy, sourceUrl=source_url, httpStatus=response.status_code, attempts=attempt)
                if response.status_code in {301, 302, 303, 307, 308, 403, 404, 407}:
                    return last
        except Exception as error:
            detail = proxy_net.safe_error(error)
            msg = f"代理出口已命中 {country}，但官网连接失败（{detail['reason']}）" if proxy.get("status") == "ok" else "官网或出口请求失败：" + detail["reason"]
            last = _channel("error", msg)
            last.update(proxy=proxy, diagnostic=detail, attempts=attempt)
            emit(f"官网 {country}：{detail['reason']}，第 {attempt}/{max_attempts} 次")
            if detail["reason"] == "proxy_auth_required":
                return last
        if attempt < max_attempts:
            time.sleep(0.5)
    return last


preflight = proxy_net.preflight
