"""Shared constants, IO helpers, FX rates, proxy env parsing, and Apple amount parser."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import html
import json
import math
import os
from pathlib import Path
import re
import threading

from curl_cffi import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "public" / "data"
FX_URL = "https://tassidicambio.bancaditalia.it/terzevalute-wf-web/rest/v1.0/latestRates?lang=en"
WRITE_LOCK = threading.RLock()
FX_LOCK = threading.Lock()

ZERO_DECIMAL = frozenset({
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
})
THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: str | Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return copy.deepcopy(default)


def write_json(path: str | Path, value, *, compact: bool = False) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK:
        tmp = target.with_suffix(target.suffix + ".tmp")
        text = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if compact
            else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)


def redact_secrets(text: str, secrets: tuple[str, ...] = ()) -> str:
    cleaned = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", str(text or ""))
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def proxy_config_from_env(environ: dict[str, str] | None = None) -> dict[str, str] | None:
    env = os.environ if environ is None else environ
    user = str(env.get("IPROYAL_PROXY_USER") or "").strip()
    password = str(env.get("IPROYAL_PROXY_PASS") or "")
    endpoint = str(env.get("IPROYAL_PROXY_ENDPOINT") or "").strip()
    if not (user and password and endpoint):
        return None
    if len(user) > 512 or len(password) > 512 or len(endpoint) > 512:
        raise ValueError("Residential proxy config exceeds 512 characters")
    match = re.fullmatch(r"[A-Za-z0-9.-]+:(\d{1,5})", endpoint)
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise ValueError("IPROYAL_PROXY_ENDPOINT must be host:port")
    return {"proxyUser": user, "proxyPass": password, "proxyEndpoint": endpoint}


def parse_apple_display_amount(display: str, currency: str) -> float:
    text = html.unescape(str(display or "")).replace("\u00a0", " ").replace("\u202f", " ").strip()
    compact = text.lower()
    if currency == "IDR" and any(word in compact for word in ("ribu", "juta")):
        number = re.sub(r"[^\d,.]", "", compact).replace(".", "").replace(",", ".")
        amount = Decimal(number) * (1_000_000 if "juta" in compact else 1_000)
    else:
        number = re.sub(r"[^\d,.\-]", "", text)
        if not number or not re.search(r"\d", number):
            raise ValueError("No numeric price")
        precision = 0 if currency in ZERO_DECIMAL else 3 if currency in THREE_DECIMAL else 2
        if precision == 0:
            number = number.replace(",", "").replace(".", "")
        elif "," in number and "." in number:
            decimal = "," if number.rfind(",") > number.rfind(".") else "."
            number = number.replace("." if decimal == "," else ",", "").replace(decimal, ".")
        else:
            separator = "," if "," in number else "." if "." in number else None
            if separator:
                pieces = number.split(separator)
                number = "".join(pieces[:-1]) + "." + pieces[-1] if len(pieces[-1]) == precision else "".join(pieces)
        amount = Decimal(number)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid non-positive price")
    return float(amount)


def normalize_fx(payload: dict) -> dict:
    entries = payload.get("latestRates", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list) or len(entries) < 100:
        raise ValueError("Incomplete official exchange rates")
    rates: dict[str, dict] = {"EUR": {"rate": 1.0, "date": None}}
    for item in entries:
        if not isinstance(item, dict):
            continue
        code = item.get("isoCode")
        date = item.get("referenceDate")
        try:
            value = float(item["eurRate"])
        except (KeyError, ValueError, TypeError):
            continue
        if re.fullmatch(r"[A-Z]{3}", str(code)) and date and math.isfinite(value) and value > 0:
            if code not in rates or not rates[code]["date"] or str(date) > str(rates[code]["date"]):
                rates[code] = {"rate": value, "date": str(date)}
    if len(rates) < 100:
        raise ValueError("Incomplete official exchange rates")
    latest_date = max(r["date"] for r in rates.values() if r["date"])
    rates["EUR"]["date"] = latest_date
    return {
        "rates": rates,
        "sourceUrl": FX_URL,
        "referenceDate": latest_date,
        "updatedAt": now_iso(),
        "error": None,
    }


def fetch_bank_of_italy_fx(
    *,
    force: bool = False,
    fx_path: Path | None = None,
    seed_path: Path | None = None,
) -> dict:
    target = Path(fx_path) if fx_path else (DATA_DIR / "fx.json")
    seed = Path(seed_path) if seed_path else (DATA_DIR / "fx-seed.json")
    with FX_LOCK:
        cached = read_json(target)
        if cached and not force:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["updatedAt"])).total_seconds()
                if 0 <= age < 1800:
                    return cached
            except (ValueError, TypeError, KeyError):
                pass
        try:
            with requests.Session(impersonate="chrome", trust_env=False) as session:
                resp = session.get(FX_URL, timeout=15, headers={"Accept": "application/json"})
                resp.raise_for_status()
                updated = normalize_fx(resp.json())
            write_json(target, updated)
            return updated
        except Exception as error:
            if not cached:
                raw_seed = read_json(seed, {})
                if isinstance(raw_seed, dict) and raw_seed.get("rates"):
                    cached = copy.deepcopy(raw_seed)
                else:
                    try:
                        cached = normalize_fx(raw_seed)
                        cached.update(updatedAt=None, reusedSnapshot=True)
                    except (ValueError, TypeError):
                        cached = {
                            "rates": {"EUR": {"rate": 1.0, "date": None}},
                            "updatedAt": None,
                            "referenceDate": None,
                            "sourceUrl": FX_URL,
                        }
            fallback = copy.deepcopy(cached)
            fallback.update(error="央行汇率更新失败，沿用已有汇率（" + type(error).__name__ + "）", stale=True)
            return fallback
