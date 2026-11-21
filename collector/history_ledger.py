"""Persistent Apple price-history ledger (90-day retention against Top-10 dropouts).

Tracks every observed (product, country, plan, currency, amount) tuple across
daily Apple patrols with firstSeenAt / lastSeenAt / seenCount so a new higher
subscription price that temporarily surfaced in Apple's Top 10 IAP list is not
lost when it later drops out or gets masked by grandfathered renewal SKUs.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
from pathlib import Path
import re
from typing import Any

from collector.common import now_iso, read_json, write_json

DEFAULT_STALE_DAYS = 90
ANNUAL_SUSPECT_RATIO = 8.5


def empty_ledger() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "staleDays": DEFAULT_STALE_DAYS,
        "updatedAt": None,
        "entries": {},
    }


def load_ledger(path: str | Path) -> dict[str, Any]:
    raw = read_json(path)
    if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
        return raw
    return empty_ledger()


def save_ledger(path: str | Path, ledger: dict[str, Any], *, compact: bool = True) -> None:
    ledger["updatedAt"] = now_iso()
    write_json(path, ledger, compact=compact)


def _parse_iso(ts: str | None) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_days(stamp: str | None, reference: datetime) -> float | None:
    dt = _parse_iso(stamp)
    if dt is None:
        return None
    return (reference - dt).total_seconds() / 86400.0


def _amount_key(currency: str, amount: float) -> str:
    return f"{currency}:{amount:.4f}".rstrip("0").rstrip(".")


def _is_valid_price_cell(cell: Any) -> bool:
    if not isinstance(cell, dict) or cell.get("status") != "ok":
        return False
    amt = cell.get("amount")
    cur = cell.get("currency")
    return (
        type(amt) in (int, float)
        and math.isfinite(amt)
        and amt > 0
        and isinstance(cur, str)
        and re.fullmatch(r"[A-Z]{3}", cur) is not None
    )


def _is_annual_suspect_jump(candidate_amount: float, reference_amount: float, entry: dict[str, Any]) -> bool:
    if reference_amount <= 0 or candidate_amount <= reference_amount:
        return False
    if candidate_amount / reference_amount < ANNUAL_SUSPECT_RATIO:
        return False
    ev = entry.get("evidence") or {}
    return ev.get("sourcePeriod") != "month"


def _timeline_view(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = sorted(
        records.values(),
        key=lambda r: (str(r.get("lastSeenAt") or ""), float(r.get("amount") or 0)),
        reverse=True,
    )
    return [
        {
            "currency": r["currency"],
            "amount": r["amount"],
            "displayPrice": r.get("displayPrice"),
            "firstSeenAt": r.get("firstSeenAt"),
            "lastSeenAt": r.get("lastSeenAt"),
            "seenCount": r.get("seenCount", 1),
            "appId": r.get("appId"),
            "appName": r.get("appName"),
        }
        for r in items[:8]
    ]


def record_and_resolve_apple_cell(
    ledger: dict[str, Any],
    *,
    product: str,
    country: str,
    plan: str,
    live_cell: dict[str, Any],
    now_at: str | None = None,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> dict[str, Any]:
    """Record live Apple observation in ledger and resolve best non-stale cell."""
    checked_at = now_at or now_iso()
    ref_dt = _parse_iso(checked_at) or datetime.now(timezone.utc)
    entries = ledger.setdefault("entries", {})
    prod_map = entries.setdefault(product, {})
    cc_map = prod_map.setdefault(country, {})
    records: dict[str, dict[str, Any]] = cc_map.setdefault(plan, {})

    result = copy.deepcopy(live_cell) if isinstance(live_cell, dict) else {"plan": plan, "status": "pending"}
    live_valid = _is_valid_price_cell(result)

    if live_valid:
        currency = result["currency"]
        amount = float(result["amount"])
        observed_at = result.get("observedAt") or checked_at
        k = _amount_key(currency, amount)
        prev = records.get(k)
        obs_dt = _parse_iso(observed_at)
        prev_dt = _parse_iso(prev.get("lastSeenAt")) if prev else None
        if prev is None:
            records[k] = {
                "currency": currency,
                "amount": amount,
                "displayPrice": result.get("displayPrice"),
                "sourceProductName": result.get("sourceProductName"),
                "appId": result.get("appId"),
                "appName": result.get("appName"),
                "sourceUrl": result.get("sourceUrl"),
                "period": result.get("period", "month"),
                "priceScope": result.get("priceScope"),
                "identityVerified": bool(result.get("identityVerified", True)),
                "periodVerified": bool(result.get("periodVerified", True)),
                "regionVerified": bool(result.get("regionVerified", True)),
                "evidence": copy.deepcopy(result.get("evidence") or {}),
                "firstSeenAt": observed_at,
                "lastSeenAt": observed_at,
                "seenCount": 1,
            }
        else:
            if prev_dt is None or (obs_dt is not None and obs_dt > prev_dt):
                prev["lastSeenAt"] = observed_at
                prev["seenCount"] = int(prev.get("seenCount") or 1) + 1
            if obs_dt is not None and (_parse_iso(prev.get("firstSeenAt")) or obs_dt) > obs_dt:
                prev["firstSeenAt"] = observed_at
            for field in ("displayPrice", "sourceProductName", "appId", "appName", "sourceUrl", "priceScope"):
                if result.get(field):
                    prev[field] = result[field]
            if isinstance(result.get("evidence"), dict) and result["evidence"]:
                prev["evidence"] = copy.deepcopy(result["evidence"])

        # Also record any distinct valid superseded grandfathered candidates if surfaced in evidence
        cur_rec = records[k]
        # Check if ledger holds a non-stale (> higher) price in the same currency within stale_days
        same_curr_fresh = [
            rec for rec in records.values()
            if rec.get("currency") == currency
            and (_age_days(rec.get("lastSeenAt"), ref_dt) or 0.0) <= stale_days
        ]
        higher_fresh = [
            rec for rec in same_curr_fresh
            if float(rec["amount"]) > amount
            and not _is_annual_suspect_jump(float(rec["amount"]), amount, rec)
        ]
        if higher_fresh:
            best = max(higher_fresh, key=lambda r: (float(r["amount"]), str(r.get("lastSeenAt") or "")))
            ev = copy.deepcopy(best.get("evidence") or {})
            sup = sorted(set(ev.get("supersededGrandfatheredPrices") or []) | {amount})
            ev["supersededGrandfatheredPrices"] = sup
            ev["priceSelection"] = "history_retained_higher_new_price"
            result.update(
                status="ok",
                plan=plan,
                amount=best["amount"],
                currency=best["currency"],
                period=best.get("period", "month"),
                sourceUrl=best.get("sourceUrl"),
                appId=best.get("appId"),
                appName=best.get("appName"),
                sourceProductName=best.get("sourceProductName"),
                displayPrice=best.get("displayPrice"),
                priceScope=best.get("priceScope") or result.get("priceScope"),
                observedAt=best.get("lastSeenAt"),
                identityVerified=bool(best.get("identityVerified", True)),
                periodVerified=bool(best.get("periodVerified", True)),
                regionVerified=bool(best.get("regionVerified", True)),
                evidence=ev,
                historyRetained=True,
                historyStale=False,
                historyFirstSeen=best.get("firstSeenAt"),
                historyLastSeen=best.get("lastSeenAt"),
                historyLastCheckedAt=checked_at,
                historySupersededLiveAmount=amount,
                excludeFromMinCard=False,
                reason=f"历史账本保留近 {stale_days} 天内更高有效新价（上次露出 {best['lastSeenAt'][:10]}；本次公开 Top 10 仅见旧低价 {result.get('displayPrice') or amount}）",
            )
        else:
            result["historyRetained"] = False
            result["historyStale"] = False
            result["historyFirstSeen"] = cur_rec.get("firstSeenAt")
            result["historyLastSeen"] = cur_rec.get("lastSeenAt")
            result["historyLastCheckedAt"] = checked_at
            result.setdefault("excludeFromMinCard", False)
        result["historyTimeline"] = _timeline_view(records)
        return result

    # No live valid price today
    # Respect explicit confirmed unavailable when no history exists or all apps returned 404
    if result.get("status") == "unavailable" and result.get("availabilityConfirmed") is True:
        if records:
            result["historyTimeline"] = _timeline_view(records)
        return result

    if not records:
        return result

    fresh_records = []
    stale_records = []
    for rec in records.values():
        age = _age_days(rec.get("lastSeenAt"), ref_dt)
        if age is not None and age <= stale_days:
            fresh_records.append(rec)
        else:
            stale_records.append(rec)

    chosen_pool = fresh_records if fresh_records else stale_records
    is_stale = not bool(fresh_records)
    chosen = max(chosen_pool, key=lambda r: (float(r["amount"]), str(r.get("lastSeenAt") or "")))
    result.update(
        status="ok",
        plan=plan,
        amount=chosen["amount"],
        currency=chosen["currency"],
        period=chosen.get("period", "month"),
        sourceUrl=chosen.get("sourceUrl"),
        appId=chosen.get("appId"),
        appName=chosen.get("appName"),
        sourceProductName=chosen.get("sourceProductName"),
        displayPrice=chosen.get("displayPrice"),
        priceScope=chosen.get("priceScope") or result.get("priceScope"),
        observedAt=chosen.get("lastSeenAt"),
        identityVerified=bool(chosen.get("identityVerified", True)),
        periodVerified=bool(chosen.get("periodVerified", True)),
        regionVerified=bool(chosen.get("regionVerified", True)),
        evidence=copy.deepcopy(chosen.get("evidence") or {}),
        historyRetained=True,
        historyStale=is_stale,
        historyFirstSeen=chosen.get("firstSeenAt"),
        historyLastSeen=chosen.get("lastSeenAt"),
        historyLastCheckedAt=checked_at,
        excludeFromMinCard=bool(is_stale),
        historyTimeline=_timeline_view(records),
        reason=(
            f"公开 Top 10 暂未露出；保留超过 {stale_days} 天的历史核验价（上次露出 {str(chosen.get('lastSeenAt') or '')[:10]}），已标灰待复核且不计入最低价看板"
            if is_stale
            else f"本次公开 Top 10 暂未露出；历史账本保留近 {stale_days} 天内已核验有效新价（上次露出 {str(chosen.get('lastSeenAt') or '')[:10]}）"
        ),
    )
    return result
