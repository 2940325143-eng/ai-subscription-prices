"""Cross-channel same-currency anomaly guard for grandfathered Apple prices."""
from __future__ import annotations

import math
import re
from typing import Iterable

DEFAULT_SUSPECT_RATIO = 0.65


def _valid_ok_amount(cell: object) -> bool:
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


def apply_grandfathered_guard(
    row: dict,
    plans: Iterable[str],
    *,
    ratio_threshold: float = DEFAULT_SUSPECT_RATIO,
) -> None:
    """Flag same-currency Apple prices abnormally below Web (<65%) as suspect grandfathered."""
    web_cells = (row.get("web") or {}).get("cells") or {}
    apple_cells = (row.get("apple") or {}).get("cells") or {}
    for plan in plans:
        w = web_cells.get(plan)
        a = apple_cells.get(plan)
        if not isinstance(a, dict):
            continue
        stale_excluded = bool(a.get("historyStale"))
        if a.get("manualOverride"):
            a["suspectGrandfathered"] = False
            a.pop("suspectRatio", None)
            a.pop("suspectReason", None)
            a["excludeFromMinCard"] = False
            continue
        if not (_valid_ok_amount(w) and _valid_ok_amount(a)):
            a["suspectGrandfathered"] = False
            a.pop("suspectRatio", None)
            a.pop("suspectReason", None)
            a["excludeFromMinCard"] = stale_excluded
            continue
        if w["currency"] != a["currency"]:
            # Cross-currency discrepancies (e.g. Web local vs Apple USD) are normal.
            a["suspectGrandfathered"] = False
            a.pop("suspectRatio", None)
            a.pop("suspectReason", None)
            a["excludeFromMinCard"] = stale_excluded
            continue
        ratio = float(a["amount"]) / float(w["amount"])
        if ratio < ratio_threshold:
            pct = round(ratio * 100)
            a["suspectGrandfathered"] = True
            a["suspectRatio"] = round(ratio, 4)
            a["excludeFromMinCard"] = True
            a["suspectReason"] = (
                f"同币种（{a['currency']}）下 App Store 公开价仅约为官网价的 {pct}%（< 65%），"
                "疑似老用户旧续订价霸榜（苹果公开 Top 10 未露出新价），已标疑并移出顶部最低价看板"
            )
        else:
            a["suspectGrandfathered"] = False
            a.pop("suspectRatio", None)
            a.pop("suspectReason", None)
            a["excludeFromMinCard"] = stale_excluded
