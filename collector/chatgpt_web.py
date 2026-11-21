"""ChatGPT 7-tier official Web collector (strict direct tax-exclusive monthly only)."""
from __future__ import annotations

import copy
import math
import re
import time
from typing import Any

from collector.common import now_iso
from collector import proxy_net

SOURCE_PAGE = "https://chatgpt.com/pricing/?type=standard"
BUSINESS_SOURCE_PAGE = "https://chatgpt.com/pricing/?type=team"
CONFIG_ENDPOINT = "https://chatgpt.com/backend-anon/checkout_pricing_config/configs/{country}"
LEGACY_CONFIG_ENDPOINT = "https://chatgpt.com/backend-api/checkout_pricing_config/configs/{country}"

MONTHLY_PRICE_BASIS = "monthly_subscription"
KNOWN_UNSUPPORTED_CODES = frozenset({"BY", "CN", "CU", "HK", "IR", "MO", "RU", "SY", "VE"})
PLANS = ("go", "plus", "businessStandard", "pro100", "businessPremium", "pro200", "pro500")
APPLE_PLANS = ("go", "plus", "pro100", "pro200", "pro500")

PLAN_CONFIG_KEYS = {
    "go": ("go",),
    "plus": ("plus",),
    "businessStandard": ("businessStandard", "business"),
    "pro100": ("pro100", "prolite", "pro5x"),
    "businessPremium": ("businessPremium", "business_prolite"),
    "pro200": ("pro200", "pro", "pro10x", "pro20x"),
    "pro500": ("pro500", "promax"),
}

MONTHLY_CONFIG_KEYS = frozenset({
    "go", "plus", "businessStandard", "business", "pro100", "prolite", "pro5x",
    "businessPremium", "business_prolite", "pro200", "pro", "pro10x", "pro20x", "pro500", "promax",
})
MONTHLY_SOURCE_PATH = re.compile(r"currency_config\.([A-Za-z][A-Za-z0-9_]*)\.month(?:\.psp_override)?\Z")


def _positive_amount(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _valid_source_path(path: Any) -> bool:
    match = MONTHLY_SOURCE_PATH.fullmatch(path) if isinstance(path, str) else None
    return match is not None and match.group(1) in MONTHLY_CONFIG_KEYS


def _conflicting_context(cell: dict) -> bool:
    return (
        cell.get("basisBlocked") is True
        or cell.get("period") not in (None, "month")
        or cell.get("priceBasis") not in (None, MONTHLY_PRICE_BASIS)
        or (cell.get("sourcePath") is not None and not _valid_source_path(cell.get("sourcePath")))
        or cell.get("isPromotional") is True
        or cell.get("is_promotional") is True
        or cell.get("isAnnual") is True
        or bool(cell.get("discount"))
    )


def valid_monthly_subscription_cell(cell: Any) -> bool:
    if not isinstance(cell, dict):
        return False
    return (
        cell.get("status") == "ok"
        and cell.get("period") == "month"
        and cell.get("priceBasis") == MONTHLY_PRICE_BASIS
        and _valid_source_path(cell.get("sourcePath"))
        and _positive_amount(cell.get("amount"))
        and cell.get("tax") in {"exclusive", "inclusive"}
        and not _conflicting_context(cell)
    )


def _safe_tax(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 32 else None


def _quote(node: dict) -> dict:
    result: dict[str, Any] = {
        "amount": node.get("amount") if _positive_amount(node.get("amount")) else None,
        "tax": _safe_tax(node.get("tax")),
    }
    override = node.get("psp_override")
    if isinstance(override, dict):
        result["psp_override"] = {
            "amount": override.get("amount") if _positive_amount(override.get("amount")) else None,
            "tax": _safe_tax(override.get("tax")),
        }
    return result


def normalize_monthly_cell(cell: Any, *, plan: str, currency: str | None = None, source_path: str | None = None, source_url: str | None = None, observed_at: str | None = None) -> dict:
    if valid_monthly_subscription_cell(cell):
        copied = copy.deepcopy(cell)
        copied["plan"] = plan
        is_inclusive = copied.get("tax") == "inclusive"
        copied["taxInclusive"] = is_inclusive
        if currency and not copied.get("currency"):
            copied["currency"] = currency
        copied.setdefault("observedAt", observed_at)
        copied.setdefault("sourceUrl", source_url)
        copied.setdefault("identityVerified", True)
        copied.setdefault("periodVerified", True)
        copied.setdefault("regionVerified", True)
        copied.setdefault(
            "priceScope",
            "official_tax_inclusive_monthly" if is_inclusive else "official_tax_exclusive_monthly",
        )
        return copied

    node = cell if isinstance(cell, dict) else {}
    supplied_path = source_path if source_path is not None else node.get("sourcePath")
    path = supplied_path if _valid_source_path(supplied_path) else None
    if path and path.endswith(".psp_override"):
        path = path[:-len(".psp_override")]

    quote = _quote(node.get("sourceQuote") if isinstance(node.get("sourceQuote"), dict) else node)
    result: dict[str, Any] = {
        "plan": plan,
        "amount": None,
        "currency": None,
        "tax": quote["tax"],
        "taxInclusive": False,
        "period": "month",
        "priceBasis": MONTHLY_PRICE_BASIS,
        "sourcePath": path,
        "sourceAmount": quote["amount"],
        "sourceTaxTreatment": quote["tax"],
        "sourceQuote": quote,
        "status": "pending_basis",
        "observedAt": observed_at,
        "sourceUrl": source_url,
        "identityVerified": False,
        "periodVerified": False,
        "regionVerified": False,
        "priceScope": "official_tax_exclusive_monthly",
        "reason": "官方月付节点未提供可直接核实的订阅标价",
    }
    raw_override = node.get("psp_override")
    context_blocked = _conflicting_context(node) or (isinstance(raw_override, dict) and _conflicting_context(raw_override))
    if path is None:
        result["reason"] = "缺少可核实的官方月付字段来源"
        if context_blocked:
            result["basisBlocked"] = True
        return result
    if context_blocked:
        result["basisBlocked"] = True
        result["reason"] = "原记录存在年付、优惠或金额口径矛盾，需重新核实月付订阅行"
        return result

    override = quote.get("psp_override")
    if isinstance(override, dict) and override.get("tax") == "exclusive" and _positive_amount(override.get("amount")):
        result.update(
            amount=float(override["amount"]),
            currency=currency,
            tax="exclusive",
            taxInclusive=False,
            sourcePath=path + ".psp_override",
            status="ok",
            identityVerified=bool(currency),
            periodVerified=True,
            regionVerified=bool(currency),
            priceScope="official_tax_exclusive_monthly",
            reason="ChatGPT 官方不含税月付标价（psp_override 核验）",
        )
    elif quote["tax"] == "exclusive" and _positive_amount(quote["amount"]):
        result.update(
            amount=float(quote["amount"]),
            currency=currency,
            tax="exclusive",
            taxInclusive=False,
            status="ok",
            identityVerified=bool(currency),
            periodVerified=True,
            regionVerified=bool(currency),
            priceScope="official_tax_exclusive_monthly",
            reason="ChatGPT 官方不含税月付标价",
        )
    elif isinstance(override, dict) and override.get("tax") == "inclusive" and _positive_amount(override.get("amount")):
        result.update(
            amount=float(override["amount"]),
            currency=currency,
            tax="inclusive",
            taxInclusive=True,
            sourcePath=path + ".psp_override",
            status="ok",
            identityVerified=bool(currency),
            periodVerified=True,
            regionVerified=bool(currency),
            priceScope="official_tax_inclusive_monthly",
            reason="OpenAI 官方该地区仅提供含税月付直出标价（tax: inclusive，无不含税节点）；已入表并参与排名",
        )
    elif quote["tax"] == "inclusive" and _positive_amount(quote["amount"]):
        result.update(
            amount=float(quote["amount"]),
            currency=currency,
            tax="inclusive",
            taxInclusive=True,
            status="ok",
            identityVerified=bool(currency),
            periodVerified=True,
            regionVerified=bool(currency),
            priceScope="official_tax_inclusive_monthly",
            reason="OpenAI 官方该地区仅提供含税月付直出标价（tax: inclusive，无不含税节点）；已入表并参与排名",
        )
    return result


def extract_config_prices(payload: dict, *, country: str, source_url: str, observed_at: str) -> dict:
    config = payload.get("currency_config")
    if not isinstance(config, dict):
        raise ValueError("missing currency_config")
    currency = config.get("symbol_code")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("invalid currency code in ChatGPT config")

    cells: dict[str, dict] = {}
    for plan, source_keys in PLAN_CONFIG_KEYS.items():
        chosen = None
        for source_key in source_keys:
            raw_plan = config.get(source_key)
            if not isinstance(raw_plan, dict):
                continue
            month_node = raw_plan.get("month")
            if not isinstance(month_node, dict):
                continue
            source_path = f"currency_config.{source_key}.month"
            override = month_node.get("psp_override")
            if _positive_amount(month_node.get("amount")) or (isinstance(override, dict) and _positive_amount(override.get("amount"))):
                chosen = normalize_monthly_cell(
                    month_node,
                    plan=plan,
                    currency=currency,
                    source_path=source_path,
                    source_url=source_url,
                    observed_at=observed_at,
                )
                break
        if chosen is None:
            chosen = normalize_monthly_cell(None, plan=plan, currency=currency, source_url=source_url, observed_at=observed_at)
            if plan == "businessPremium":
                chosen["reason"] = "官方配置未提供 Business Premium 不含税月付订阅行"
        cells[plan] = chosen

    return {
        "currency": currency,
        "symbol": config.get("symbol"),
        "minorUnitExponent": config.get("minor_unit_exponent"),
        "cells": cells,
    }


def fetch_country(
    country: str,
    config: dict[str, str],
    transport,
    *,
    stop=None,
    emit=lambda m: None,
    timeout: int = 35,
    retries: int = 4,
) -> dict:
    code = str(country).upper()
    if code in KNOWN_UNSUPPORTED_CODES:
        return {
            "status": "unavailable",
            "availabilityConfirmed": True,
            "reason": "ChatGPT 官方不支持该地区（KNOWN_UNSUPPORTED_CODES）",
            "proxy": {"status": "skipped", "reason": "unsupported_country"},
            "cells": {
                p: {
                    "plan": p,
                    "status": "unavailable",
                    "availabilityConfirmed": True,
                    "amount": None,
                    "currency": None,
                    "reason": "ChatGPT 不支持该地区",
                }
                for p in PLANS
            },
        }
    if transport is None:
        return {
            "status": "error",
            "reason": "住宅代理入口未通过预检",
            "proxy": {"status": "error", "reason": "gateway_unavailable"},
            "cells": {p: normalize_monthly_cell(None, plan=p) for p in PLANS},
        }

    max_attempts = max(1, min(5, retries))
    last_reason = "请求失败"
    last_proxy: dict = {"status": "error", "reason": "request_failed"}

    for attempt in range(1, max_attempts + 1):
        if stop and stop.is_set():
            return {
                "status": "error",
                "reason": "已取消",
                "proxy": last_proxy,
                "cells": {p: normalize_monthly_cell(None, plan=p) for p in PLANS},
            }
        emit(f"ChatGPT 官网 {code}：第 {attempt}/{max_attempts} 次（住宅代理）")
        session = None
        proxy_info = {"status": "ok", "actualCountry": code, "attempts": attempt}
        try:
            kwargs = proxy_net.proxy_kwargs(transport, config, code, proxy_net.new_session_id(), lifetime="5m")
            session = proxy_net.create_session(transport)
            geo = proxy_net.verify_geo(session, kwargs, code, timeout=min(12, timeout))
            proxy_info.update(geo)
            if geo.get("status") != "ok":
                last_proxy = proxy_info
                last_reason = "住宅代理出口不匹配" if geo.get("status") == "mismatch" else "住宅代理无法连通"
                if geo.get("reason") == "proxy_auth_required":
                    break
                time.sleep(0.6)
                continue

            session.get(SOURCE_PAGE, timeout=min(25, timeout), allow_redirects=True, headers={"Accept-Language": "en-US,en;q=0.9"}, **kwargs)
            session.get(BUSINESS_SOURCE_PAGE, timeout=min(25, timeout), allow_redirects=True, headers={"Accept-Language": "en-US,en;q=0.9"}, **kwargs)

            url = CONFIG_ENDPOINT.format(country=code)
            cfg_resp = session.get(
                url,
                timeout=min(25, timeout),
                allow_redirects=True,
                headers={"Accept": "application/json", "Accept-Language": "en-US,en;q=0.9", "Referer": BUSINESS_SOURCE_PAGE},
                **kwargs,
            )
            source_used = url
            if cfg_resp.status_code != 200:
                legacy_url = LEGACY_CONFIG_ENDPOINT.format(country=code)
                legacy_resp = session.get(
                    legacy_url,
                    timeout=min(25, timeout),
                    allow_redirects=True,
                    headers={"Accept": "application/json", "Accept-Language": "en-US,en;q=0.9", "Referer": SOURCE_PAGE},
                    **kwargs,
                )
                if legacy_resp.status_code == 200:
                    cfg_resp = legacy_resp
                    source_used = legacy_url

            if cfg_resp.status_code != 200:
                raise RuntimeError(f"ChatGPT config HTTP {cfg_resp.status_code}")

            payload = cfg_resp.json()
            if payload.get("country_code") != code:
                raise ValueError(f"country mismatch: {payload.get('country_code')!r}")

            observed_at = now_iso()
            extracted = extract_config_prices(payload, country=code, source_url=source_used, observed_at=observed_at)
            ok_count = sum(c["status"] == "ok" for c in extracted["cells"].values())
            emit(f"ChatGPT 官网 {code}：核验完成 {ok_count}/{len(PLANS)} 档 ({extracted['currency']})")
            return {
                "status": "ok" if ok_count else "pending",
                "observedAt": observed_at,
                "sourceUrl": source_used,
                "currency": extracted["currency"],
                "proxy": proxy_info,
                "attempts": attempt,
                "cells": extracted["cells"],
                "reason": "ChatGPT 官方不含税月付配置已核验" if ok_count else "无不含税月付订阅行",
            }
        except Exception as err:
            detail = proxy_net.safe_error(err)
            last_proxy = proxy_info
            last_reason = detail.get("reason") or type(err).__name__
            if detail.get("reason") == "proxy_auth_required":
                break
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
        if attempt < max_attempts:
            time.sleep(min(6.0, 0.7 * attempt))

    return {
        "status": "error",
        "observedAt": now_iso(),
        "proxy": last_proxy,
        "reason": f"ChatGPT 官网请求失败：{last_reason}",
        "cells": {p: {**normalize_monthly_cell(None, plan=p), "status": "error", "reason": last_reason} for p in PLANS},
    }


preflight = proxy_net.preflight
