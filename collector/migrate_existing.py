"""One-shot read-only migration from existing 18767 and 18771 snapshots."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

from collector import chatgpt_apple, chatgpt_web
from collector.common import DATA_DIR, now_iso, read_json, write_json
from collector.history_ledger import empty_ledger, save_ledger
from collector.publisher import LEDGER_PATH, OVERRIDES_PATH, PUBLISHED_PATH, build_published_snapshot, empty_overrides, save_published

OLD_ROOT = Path("/Users/zouchenlong/Downloads/定价")
OLD_GOOGLE_ROOT = OLD_ROOT / "google-ai-ultra-prices"


def _extract_js_var(html_text: str, name: str):
    match = re.search(rf"const {re.escape(name)} = (?:new Set\()?", html_text)
    if not match:
        raise ValueError(f"Cannot find JS constant {name}")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(html_text[match.end():])
    return obj


def _normalize_legacy_app210_plan_key(key: str) -> str:
    mapping = {
        "go": "go",
        "plus": "plus",
        "pro100": "pro100",
        "pro5x": "pro100",
        "pro200": "pro200",
        "pro10x": "pro200",
        "pro20x": "pro200",
        "pro500": "pro500",
    }
    return mapping.get(key, key)


def migrate() -> dict:
    iso_html = (OLD_ROOT / "iso-countries.html").read_text(encoding="utf-8")
    iso_countries = _extract_js_var(iso_html, "ISO_COUNTRIES")
    ipr_codes = set(_extract_js_var(iso_html, "IPROYAL_COUNTRY_CODES"))
    price_results = _extract_js_var(iso_html, "PRICE_RESULTS")
    app210 = read_json(OLD_ROOT / "app-store-prices-210.json")
    app_results = (app210 or {}).get("results") or _extract_js_var(iso_html, "APP_STORE_RESULTS")
    proxy_results_map = _extract_js_var(iso_html, "PROXY_RESULTS")
    price_checked_at = _extract_js_var(iso_html, "PRICE_CHECKED_AT")
    app_checked_at = _extract_js_var(iso_html, "APP_STORE_CHECKED_AT")

    iso_lookup = {entry[0]: {"name": entry[1], "flag": entry[2]} for entry in iso_countries if len(entry) >= 3}

    old_countries = read_json(OLD_GOOGLE_ROOT / "data" / "countries.json", {})
    old_google_pub = read_json(OLD_GOOGLE_ROOT / "data" / "published.json", {})
    old_google_apps = read_json(OLD_GOOGLE_ROOT / "data" / "apple-google-apps.json")
    old_apple_policy = read_json(OLD_GOOGLE_ROOT / "data" / "apple-policy.json")
    old_web_policy = read_json(OLD_GOOGLE_ROOT / "data" / "web-price-policy.json")
    old_fx = read_json(OLD_GOOGLE_ROOT / "data" / "fx.json")
    old_fx_seed = read_json(OLD_GOOGLE_ROOT / "data" / "fx-seed.json")

    if old_countries:
        write_json(DATA_DIR / "countries.json", old_countries)
    if old_google_apps:
        # Compact noIapApps slightly if needed while preserving all audit metadata
        write_json(DATA_DIR / "apple-google-apps.json", old_google_apps)
    if old_apple_policy:
        write_json(DATA_DIR / "apple-policy.json", old_apple_policy)
    if old_web_policy:
        write_json(DATA_DIR / "web-price-policy.json", old_web_policy)
    if old_fx:
        write_json(DATA_DIR / "fx.json", old_fx)
    if old_fx_seed:
        write_json(DATA_DIR / "fx-seed.json", old_fx_seed)

    # Build ChatGPT countries list = 199 IPRoyal + 11 extra Apple storefronts (210 total)
    cg_codes = sorted(set(ipr_codes) | set(app_results.keys()) | set(price_results.keys()))
    chatgpt_rows = []
    for code in cg_codes:
        meta = iso_lookup.get(code, {"name": code, "flag": ""})
        in_proxy = code in ipr_codes
        raw_proxy = proxy_results_map.get(code)
        if not in_proxy:
            proxy_cell = {"status": "not_in_proxy_catalog", "reason": "仅 Apple 扩展商店地区，无住宅代理节点"}
        elif isinstance(raw_proxy, dict):
            proxy_cell = {"status": raw_proxy.get("status", "ok"), "actualCountry": code if raw_proxy.get("status") == "ok" else None, "reason": raw_proxy.get("reason") or "已核验"}
        else:
            proxy_cell = {"status": "ok", "actualCountry": code, "reason": "IPRoyal 住宅代理支持"}

        # Web channel from 18767 PRICE_RESULTS
        rw = price_results.get(code)
        if code in chatgpt_web.KNOWN_UNSUPPORTED_CODES:
            web_channel = {
                "status": "unavailable",
                "availabilityConfirmed": True,
                "reason": "ChatGPT 官方不支持该地区（KNOWN_UNSUPPORTED_CODES）",
                "observedAt": (rw or {}).get("priceUpdatedAt") or price_checked_at,
                "cells": {
                    p: {"plan": p, "status": "unavailable", "availabilityConfirmed": True, "amount": None, "currency": None, "reason": "ChatGPT 不支持该地区"}
                    for p in chatgpt_web.PLANS
                },
            }
        elif not in_proxy:
            web_channel = {
                "status": "unavailable",
                "availabilityConfirmed": True,
                "reason": "不在 199 国住宅代理目录中（仅采集 App Store）",
                "cells": {
                    p: {"plan": p, "status": "unavailable", "availabilityConfirmed": True, "amount": None, "currency": None, "reason": "无对应住宅代理"}
                    for p in chatgpt_web.PLANS
                },
            }
        elif not isinstance(rw, dict) or rw.get("status") != "ok":
            web_channel = {
                "status": "error" if (rw or {}).get("status") == "failed" else "pending",
                "observedAt": (rw or {}).get("refreshAttemptedAt") or price_checked_at,
                "reason": (rw or {}).get("refreshError") or "18767 迁移时尚未成功抓取官网配置",
                "cells": {p: chatgpt_web.normalize_monthly_cell(None, plan=p) for p in chatgpt_web.PLANS},
            }
        else:
            cur = rw.get("currency")
            obs = rw.get("priceUpdatedAt") or price_checked_at
            src_url = chatgpt_web.CONFIG_ENDPOINT.format(country=code)
            raw_prices = rw.get("prices") or {}
            cells = {}
            for plan in chatgpt_web.PLANS:
                node = raw_prices.get(plan)
                cells[plan] = chatgpt_web.normalize_monthly_cell(
                    node,
                    plan=plan,
                    currency=cur,
                    source_url=src_url,
                    observed_at=obs,
                )
            ok_cnt = sum(c["status"] == "ok" for c in cells.values())
            web_channel = {
                "status": "ok" if ok_cnt else "pending",
                "observedAt": obs,
                "sourceUrl": src_url,
                "currency": cur,
                "reason": "18767 已核验不含税直出月付配置迁移" if ok_cnt else "官方节点均含税或缺失",
                "cells": cells,
            }

        # Apple channel from app-store-prices-210.json (re-evaluated with upgraded max + >=8.5x rule!)
        ra = app_results.get(code)
        if not isinstance(ra, dict):
            apple_channel = {
                "status": "pending",
                "reason": "尚未采集 App Store",
                "cells": {p: {"plan": p, "status": "pending", "amount": None, "currency": None} for p in chatgpt_apple.PLANS},
            }
        else:
            st = ra.get("status")
            cur = ra.get("currency")
            src_url = ra.get("sourceUrl") or f"https://apps.apple.com/{code.lower()}/app/chatgpt/id{chatgpt_apple.APP_ID}"
            if st == "not_available":
                apple_channel = {
                    "status": "unavailable",
                    "availabilityConfirmed": True,
                    "httpStatus": 404,
                    "observedAt": app_checked_at,
                    "reason": "Apple 确认该地区未上架 ChatGPT（HTTP 404）",
                    "cells": {
                        p: {"plan": p, "status": "unavailable", "availabilityConfirmed": True, "amount": None, "currency": None, "reason": "未上架"}
                        for p in chatgpt_apple.PLANS
                    },
                }
            elif st == "no_storefront":
                target = ra.get("redirectedTo") or "US"
                apple_channel = {
                    "status": "pending",
                    "finalStorefront": target,
                    "observedAt": app_checked_at,
                    "reason": f"Apple 跨区重定向至 {target}；不采用跨区价格",
                    "cells": {
                        p: {"plan": p, "status": "pending", "amount": None, "currency": None, "reason": f"跨区重定向至 {target}"}
                        for p in chatgpt_apple.PLANS
                    },
                }
            else:
                raw_cands = ra.get("candidates") or {}
                raw_prods = ra.get("products") or {}
                grouped: dict[str, list[dict]] = {p: [] for p in chatgpt_apple.PLANS}
                for rk, cand_list in raw_cands.items():
                    canon = _normalize_legacy_app210_plan_key(rk)
                    if canon in grouped and isinstance(cand_list, list):
                        for item in cand_list:
                            if isinstance(item, dict):
                                grouped[canon].append({
                                    "sourceProductName": item.get("sourceProductName"),
                                    "displayPrice": item.get("displayPrice"),
                                    "amount": item.get("amount"),
                                    "currency": cur,
                                    "appId": chatgpt_apple.APP_ID,
                                    "appName": chatgpt_apple.APP_NAME,
                                    "sourceUrl": src_url,
                                    "observedAt": app_checked_at,
                                })
                # Fallback if candidates absent for any plan present in products
                for rk, prod_item in raw_prods.items():
                    canon = _normalize_legacy_app210_plan_key(rk)
                    if canon in grouped and not grouped[canon] and isinstance(prod_item, dict) and prod_item.get("amount"):
                        grouped[canon].append({
                            "sourceProductName": prod_item.get("sourceProductName") or f"ChatGPT {canon}",
                            "displayPrice": prod_item.get("displayPrice"),
                            "amount": prod_item.get("amount"),
                            "currency": cur,
                            "appId": chatgpt_apple.APP_ID,
                            "appName": chatgpt_apple.APP_NAME,
                            "sourceUrl": src_url,
                            "observedAt": app_checked_at,
                        })
                cells = {}
                for plan in chatgpt_apple.PLANS:
                    cell, _ = chatgpt_apple.select_plan_candidates(grouped[plan], plan, currency=cur)
                    cells[plan] = cell
                ok_cnt = sum(c["status"] == "ok" for c in cells.values())
                apple_channel = {
                    "status": "ok" if ok_cnt == len(chatgpt_apple.PLANS) else "partial" if ok_cnt else "pending",
                    "observedAt": app_checked_at,
                    "sourceUrl": src_url,
                    "currency": cur,
                    "reason": "从 18767 App Store 210 地区快照迁移并应用年付隔离与最高新价规则",
                    "cells": cells,
                }

        chatgpt_rows.append({
            "code": code,
            "name": meta["name"],
            "flag": meta["flag"],
            "inProxyCatalog": in_proxy,
            "proxy": proxy_cell,
            "web": web_channel,
            "apple": apple_channel,
            "excluded": False,
        })

    # Google AI Ultra from 18771 published.json
    google_rows = []
    for row in copy.deepcopy(old_google_pub.get("countries") or []):
        row.setdefault("inProxyCatalog", True)
        google_rows.append(row)

    google_prod = {
        "product": "google_ultra",
        "title": "Google AI Ultra",
        "webUpdatedAt": old_google_pub.get("updatedAt"),
        "appleUpdatedAt": old_google_pub.get("updatedAt"),
        "appleCatalog": old_google_apps or old_google_pub.get("appleCatalog"),
        "countries": google_rows,
    }
    chatgpt_prod = {
        "product": "chatgpt",
        "title": "ChatGPT",
        "webUpdatedAt": price_checked_at,
        "appleUpdatedAt": app_checked_at,
        "countries": chatgpt_rows,
    }

    ledger = empty_ledger()
    overrides = load_or_init_overrides()
    fx = old_fx or {"rates": {"EUR": {"rate": 1.0, "date": None}}, "updatedAt": now_iso(), "referenceDate": None}

    snapshot = build_published_snapshot(
        {"chatgpt": chatgpt_prod, "google_ultra": google_prod},
        ledger=ledger,
        overrides=overrides,
        fx=fx,
        update_apple_ledger_for={"chatgpt", "google_ultra"},
    )
    save_ledger(LEDGER_PATH, ledger)
    save_published(snapshot, PUBLISHED_PATH)
    return snapshot


def load_or_init_overrides():
    if OVERRIDES_PATH.exists():
        raw = read_json(OVERRIDES_PATH)
        if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
            return raw
    ov = empty_overrides()
    write_json(OVERRIDES_PATH, ov)
    return ov


if __name__ == "__main__":
    snap = migrate()
    cg = snap["products"]["chatgpt"]["summary"]
    gu = snap["products"]["google_ultra"]["summary"]
    print(f"Migrated ChatGPT: countries={cg['candidateCount']}, webValid={cg['webValidCells']}, appleValid={cg['appleValidCells']}, suspect={cg['suspectCells']}")
    print(f"Migrated Google Ultra: countries={gu['candidateCount']}, webValid={gu['webValidCells']}, appleValid={gu['appleValidCells']}, suspect={gu['suspectCells']}")
    print(f"Saved published.json bytes: {PUBLISHED_PATH.stat().st_size}")
