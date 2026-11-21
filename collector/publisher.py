"""Unified dataset builder, validator, override applier, ledger integration, and summary computation."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import re
from typing import Any

from collector.anomaly_guard import apply_grandfathered_guard
from collector.common import DATA_DIR, now_iso, read_json, write_json
from collector.history_ledger import record_and_resolve_apple_cell
from collector import chatgpt_apple, chatgpt_web, google_apple, google_web

CHATGPT_PLANS = chatgpt_web.PLANS
CHATGPT_APPLE_PLANS = chatgpt_apple.PLANS
GOOGLE_PLANS = google_apple.PLANS

OVERRIDES_PATH = DATA_DIR / "overrides.json"
LEDGER_PATH = DATA_DIR / "apple-history.json"
PUBLISHED_PATH = DATA_DIR / "published.json"


def empty_overrides() -> dict[str, Any]:
    return {"schemaVersion": 1, "updatedAt": None, "entries": {}}


def load_overrides(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else OVERRIDES_PATH
    raw = read_json(target)
    if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
        return raw
    return empty_overrides()


def override_key(product: str, country: str, channel: str, plan: str) -> str:
    return f"{product}:{country}:{channel}:{plan}"


def compact_google_app_entry(app: dict) -> dict:
    if not isinstance(app, dict):
        return {}
    out = {
        key: app[key]
        for key in (
            "appId",
            "appName",
            "status",
            "httpStatus",
            "finalStorefront",
            "observedAt",
            "lastCheckedAt",
            "skippedStorefrontRedirect",
            "retainedVerified",
        )
        if key in app and app[key] is not None
    }
    products = app.get("products")
    if isinstance(products, list) and products:
        compact_products = []
        for p in products:
            if not isinstance(p, dict):
                continue
            compact_products.append({
                k: p[k]
                for k in (
                    "plan",
                    "status",
                    "amount",
                    "currency",
                    "displayPrice",
                    "sourceProductName",
                    "isGrandfathered",
                    "excludedAsGrandfathered",
                )
                if k in p and p[k] is not None
            })
        if compact_products:
            out["products"] = compact_products
    return out


def compact_cell_candidates(candidates: list | None) -> list[dict]:
    if not isinstance(candidates, list):
        return []
    compact = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        item = {
            k: c[k]
            for k in (
                "plan",
                "status",
                "amount",
                "currency",
                "sourceAmount",
                "sourceCurrency",
                "displayPrice",
                "sourceProductName",
                "appId",
                "appName",
                "sourceUrl",
                "observedAt",
                "sourcePeriod",
                "isPromotional",
                "isAnnual",
                "isGrandfathered",
                "excludedAsAnnual",
                "excludedAsGrandfathered",
                "supersededByAmount",
                "retainedVerified",
                "reason",
            )
            if k in c and c[k] is not None
        }
        ev = c.get("evidence")
        if isinstance(ev, dict):
            ev_small = {
                k: ev[k]
                for k in ("excludedAsGrandfathered", "supersededByAmount", "sourcePeriod", "identityBasis")
                if k in ev and ev[k] is not None
            }
            if ev_small:
                item["evidence"] = ev_small
        compact.append(item)
    return compact


def apply_override_to_cell(cell: dict, entry: dict) -> dict:
    updated = copy.deepcopy(cell) if isinstance(cell, dict) else {}
    currency = str(entry.get("currency") or "").upper()
    amount = entry.get("amount")
    if not (re.fullmatch(r"[A-Z]{3}", currency) and type(amount) in (int, float) and math.isfinite(amount) and amount > 0):
        return updated
    if updated.get("amount") is not None and not updated.get("manualOverride"):
        updated["rawObserved"] = {
            "amount": updated.get("amount"),
            "currency": updated.get("currency"),
            "displayPrice": updated.get("displayPrice"),
            "observedAt": updated.get("observedAt"),
            "reason": updated.get("reason"),
        }
    updated.update(
        status="ok",
        amount=float(amount),
        currency=currency,
        period="month",
        identityVerified=True,
        periodVerified=True,
        regionVerified=True,
        manualOverride=True,
        manualOverrideNote=str(entry.get("note") or "管理员实测校准价"),
        manualOverrideAt=entry.get("updatedAt") or now_iso(),
        observedAt=entry.get("updatedAt") or updated.get("observedAt") or now_iso(),
        suspectGrandfathered=False,
        excludeFromMinCard=False,
        reason=f"实测校准价：{entry.get('note') or '已人工核验新开月付价'}",
    )
    updated.pop("suspectRatio", None)
    updated.pop("suspectReason", None)
    return updated


def _cell_is_usable(product: str, channel: str, plan: str, cell: Any, country: str, allowed_google_apps=None) -> bool:
    if not isinstance(cell, dict) or cell.get("status") != "ok":
        return False
    amt = cell.get("amount")
    cur = cell.get("currency")
    if not (type(amt) in (int, float) and math.isfinite(amt) and amt > 0 and re.fullmatch(r"[A-Z]{3}", str(cur or ""))):
        return False
    if cell.get("manualOverride"):
        return True
    if product == "google_ultra":
        if channel == "web":
            return google_web.valid_web_price(cell, country)
        if channel == "apple":
            return google_apple.valid_apple_price(cell, country, allowed_apps=allowed_google_apps)
        return False
    if product == "chatgpt":
        if channel == "web":
            return chatgpt_web.valid_monthly_subscription_cell(cell)
        if channel == "apple":
            return (
                plan in CHATGPT_APPLE_PLANS
                and cell.get("period") == "month"
                and not cell.get("isPromotional")
                and not cell.get("isAnnual")
            )
    return False


def _finalize_product(
    product: str,
    rows: list[dict],
    plans: tuple[str, ...],
    apple_plans: tuple[str, ...],
    *,
    ledger: dict[str, Any],
    overrides: dict[str, Any],
    now_at: str,
    allowed_google_apps=None,
    update_ledger_for_apple: bool = True,
) -> dict[str, Any]:
    counts = {
        "candidateCount": len(rows),
        "iproyalCount": sum(1 for r in rows if r.get("inProxyCatalog", True)),
        "extraStorefrontCount": sum(1 for r in rows if not r.get("inProxyCatalog", True)),
        "displayedCount": 0,
        "excludedCount": 0,
        "validRegions": 0,
        "pendingRegions": 0,
        "webValidCells": 0,
        "appleValidCells": 0,
        "pendingCells": 0,
        "errorCells": 0,
        "suspectCells": 0,
        "historyRetainedCells": 0,
        "historyStaleCells": 0,
        "overrideCells": 0,
    }
    override_entries = (overrides or {}).get("entries") or {}

    for row in rows:
        cc = row["code"]
        web_ch = row.setdefault("web", {"status": "pending", "cells": {}})
        web_cells = web_ch.setdefault("cells", {})

        # Google Web 5x >= 20x conflict guard
        if product == "google_ultra":
            w5 = web_cells.get("ultra5x")
            w20 = web_cells.get("ultra20x")
            if (
                _cell_is_usable(product, "web", "ultra5x", w5, cc)
                and _cell_is_usable(product, "web", "ultra20x", w20, cc)
                and w5.get("currency") == w20.get("currency")
                and w5.get("amount", 0) >= w20.get("amount", 0)
            ):
                raw_amount = w5.get("amount")
                ev = w5.setdefault("evidence", {}) if isinstance(w5.get("evidence"), dict) else {}
                ev["currencyAmountConflict"] = True
                ev["rawAmount"] = raw_amount
                w5.update(
                    status="pending",
                    amount=None,
                    currency=None,
                    reason="官网价格源币种与数值冲突（5× 标价不低于 20×），已拦截不计入",
                )

        apple_ch = row.setdefault("apple", {"status": "pending", "cells": {}})
        apple_cells = apple_ch.setdefault("cells", {})

        # Validate & resolve Apple via ledger
        for plan in apple_plans:
            cell = apple_cells.get(plan) or {"plan": plan, "status": "pending"}
            # Compact candidates before storing
            if isinstance(cell.get("candidates"), list):
                cell["candidates"] = compact_cell_candidates(cell["candidates"])
            if not _cell_is_usable(product, "apple", plan, cell, cc, allowed_google_apps=allowed_google_apps):
                if cell.get("status") == "ok" and not cell.get("manualOverride"):
                    cell.update(status="pending", amount=None, currency=None, reason="套餐、月付账期或当前地区证据未通过校验")
                elif cell.get("status") not in {"ok", "unavailable"}:
                    cell.update(amount=None, currency=None)
            if update_ledger_for_apple and not cell.get("manualOverride"):
                cell = record_and_resolve_apple_cell(
                    ledger,
                    product=product,
                    country=cc,
                    plan=plan,
                    live_cell=cell,
                    now_at=now_at,
                )
            elif not cell.get("manualOverride"):
                # Still apply read-only resolution if needed when non-apple updates run
                existing_records = (
                    ledger.get("entries", {}).get(product, {}).get(cc, {}).get(plan, {})
                )
                if existing_records and (cell.get("status") != "ok" or any(
                    float(rec.get("amount") or 0) > float(cell.get("amount") or 0)
                    for rec in existing_records.values()
                    if rec.get("currency") == cell.get("currency")
                )):
                    cell = record_and_resolve_apple_cell(
                        ledger,
                        product=product,
                        country=cc,
                        plan=plan,
                        live_cell=cell,
                        now_at=now_at,
                    )
            apple_cells[plan] = cell

        # Compact google_ultra per-app metadata and hoist duplicated per-row policy
        if product == "google_ultra":
            apple_ch.pop("policy", None)
            if isinstance(apple_ch.get("apps"), list):
                apple_ch["apps"] = [compact_google_app_entry(a) for a in apple_ch["apps"]]

        # Validate Web cells
        for plan in plans:
            wcell = web_cells.get(plan) or {"plan": plan, "status": "pending", "amount": None, "currency": None}
            if not _cell_is_usable(product, "web", plan, wcell, cc):
                if wcell.get("status") == "ok" and not wcell.get("manualOverride"):
                    wcell.update(status="pending", amount=None, currency=None, reason="官网月付或不含税/当地依据未通过校验")
                elif wcell.get("status") not in {"ok"}:
                    wcell.update(amount=None, currency=None)
            web_cells[plan] = wcell

        # Apply manual overrides (both channels)
        for plan in plans:
            w_ov = override_entries.get(override_key(product, cc, "web", plan))
            if isinstance(w_ov, dict):
                web_cells[plan] = apply_override_to_cell(web_cells[plan], w_ov)
            if plan in apple_plans:
                a_ov = override_entries.get(override_key(product, cc, "apple", plan))
                if isinstance(a_ov, dict):
                    apple_cells[plan] = apply_override_to_cell(apple_cells[plan], a_ov)

        # Apply same-currency cross-channel anomaly guard (< 65% of Web)
        apply_grandfathered_guard(row, apple_plans)

        # Compute counts
        usable_here = 0
        pending_here = False
        for plan in plans:
            wc = web_cells[plan]
            if wc.get("status") == "ok" and wc.get("amount") is not None:
                usable_here += 1
                counts["webValidCells"] += 1
                if wc.get("manualOverride"):
                    counts["overrideCells"] += 1
            else:
                uncertain = wc.get("status") not in {"unavailable"}
                pending_here |= uncertain
                counts["pendingCells"] += int(uncertain)
                counts["errorCells"] += int(wc.get("status") in {"error", "failed"})

        for plan in apple_plans:
            ac = apple_cells[plan]
            if ac.get("status") == "ok" and ac.get("amount") is not None:
                usable_here += 1
                counts["appleValidCells"] += 1
                if ac.get("suspectGrandfathered"):
                    counts["suspectCells"] += 1
                if ac.get("historyRetained"):
                    counts["historyRetainedCells"] += 1
                if ac.get("historyStale"):
                    counts["historyStaleCells"] += 1
                if ac.get("manualOverride"):
                    counts["overrideCells"] += 1
            else:
                uncertain = ac.get("status") not in {"unavailable"}
                pending_here |= uncertain
                counts["pendingCells"] += int(uncertain)
                counts["errorCells"] += int(ac.get("status") in {"error", "failed"})

        absent = (
            row.get("web", {}).get("status") == "unavailable"
            and row.get("web", {}).get("availabilityConfirmed") is True
            and row.get("apple", {}).get("status") == "unavailable"
            and row.get("apple", {}).get("availabilityConfirmed") is True
        )
        row["excluded"] = bool(not usable_here and absent)
        if row["excluded"]:
            row["excludedReason"] = "官网及 App Store 均确认不支持或未上架"
            counts["excludedCount"] += 1
        else:
            row.pop("excludedReason", None)
            counts["displayedCount"] += 1
            counts["validRegions"] += int(bool(usable_here))
            counts["pendingRegions"] += int(pending_here)

    return counts


def build_published_snapshot(
    raw_products: dict[str, dict],
    *,
    ledger: dict[str, Any],
    overrides: dict[str, Any],
    fx: dict[str, Any],
    update_apple_ledger_for: set[str] | None = None,
) -> dict[str, Any]:
    now_at = now_iso()
    update_set = {"chatgpt", "google_ultra"} if update_apple_ledger_for is None else update_apple_ledger_for

    google_prod = copy.deepcopy(raw_products["google_ultra"])
    chatgpt_prod = copy.deepcopy(raw_products["chatgpt"])

    # Determine allowed google apps dynamically from catalog + baseline
    allowed_google = set(google_apple.APPLE_APPS)
    cat = google_prod.get("appleCatalog")
    if isinstance(cat, dict) and isinstance(cat.get("apps"), dict):
        allowed_google.update(str(k) for k in cat["apps"] if re.fullmatch(r"\d+", str(k)))

    google_summary = _finalize_product(
        "google_ultra",
        google_prod["countries"],
        GOOGLE_PLANS,
        GOOGLE_PLANS,
        ledger=ledger,
        overrides=overrides,
        now_at=now_at,
        allowed_google_apps=allowed_google,
        update_ledger_for_apple="google_ultra" in update_set,
    )
    google_prod["summary"] = google_summary
    google_prod.setdefault("plans", list(GOOGLE_PLANS))
    google_prod.setdefault("applePlans", list(GOOGLE_PLANS))
    google_prod["updatedAt"] = now_at

    chatgpt_summary = _finalize_product(
        "chatgpt",
        chatgpt_prod["countries"],
        CHATGPT_PLANS,
        CHATGPT_APPLE_PLANS,
        ledger=ledger,
        overrides=overrides,
        now_at=now_at,
        update_ledger_for_apple="chatgpt" in update_set,
    )
    chatgpt_prod["summary"] = chatgpt_summary
    chatgpt_prod.setdefault("plans", list(CHATGPT_PLANS))
    chatgpt_prod.setdefault("applePlans", list(CHATGPT_APPLE_PLANS))
    chatgpt_prod["updatedAt"] = now_at

    # Trim fx payload slightly if needed while keeping all rates
    fx_clean = {
        "sourceUrl": fx.get("sourceUrl"),
        "referenceDate": fx.get("referenceDate"),
        "updatedAt": fx.get("updatedAt"),
        "rates": fx.get("rates", {}),
    }

    return {
        "schemaVersion": 2,
        "updatedAt": now_at,
        "patrolSchedule": "每天北京时间 04:00 自动直连巡检 Apple（零住宅代理流量）",
        "fx": fx_clean,
        "products": {
            "chatgpt": chatgpt_prod,
            "google_ultra": google_prod,
        },
    }


def save_published(snapshot: dict[str, Any], path: str | Path | None = None) -> None:
    write_json(path or PUBLISHED_PATH, snapshot, compact=True)
