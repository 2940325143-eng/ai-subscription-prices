"""Google-owned public Apple storefront catalog discovery and shared Apple rate limiter."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlparse

from curl_cffi import requests

from collector.common import DATA_DIR, now_iso, read_json, write_json

GOOGLE_DEVELOPER_ID = "281956209"
GOOGLE_DEVELOPER_NAME = "Google"
GOOGLE_SELLER_NAME = "Google LLC"
CATALOG_PATH = DATA_DIR / "apple-google-apps.json"
SEED_STOREFRONTS = ("us", "gb", "jp", "in", "tr", "de", "br", "sg", "au", "kr")
PRIORITY_APP_IDS = (
    "6477489729",  # Google Gemini
    "1451784328",  # Google One
    "284815942",   # Google
    "6737527615",  # Gemini Notebook
    "909319292",   # Google Calendar
    "535886823",   # Google Chrome
    "842842640",   # Google Docs
    "422689480",   # Gmail
    "962194608",   # Google Photos
    "507874739",   # Google Drive
    "585027354",   # Google Maps
    "842849113",   # Google Sheets
    "879478102",   # Google Slides
    "1096918571",  # Google Meet
    "1163852619",  # Google Chat
    "1029207872",  # Google Keep
)

APPLE_APPS = {
    "6477489729": "Google Gemini",
    "1451784328": "Google One",
    "284815942": "Google",
    "422689480": "Gmail",
    "962194608": "Google Photos",
    "507874739": "Google Drive",
    "6737527615": "Gemini Notebook",
    "585027354": "Google Maps",
}

_CATALOG_LOCK = threading.RLock()
_CATALOG = None


class AppleRateLimiter:
    """Global concurrency + adaptive pacing limiter for apps.apple.com."""

    def __init__(self, max_concurrency: int = 4, min_interval: float = 0.25):
        self._sem = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self._lock = threading.Lock()
        self._base_interval = max(0.0, float(min_interval))
        self._interval = self._base_interval
        self._next_allowed = 0.0
        self._cooldown_until = 0.0
        self._successes = 0

    def acquire(self, stop=None) -> bool:
        while True:
            if stop and stop.is_set():
                return False
            if self._sem.acquire(timeout=0.5):
                break
        with self._lock:
            now_ts = time.monotonic()
            ready_at = max(now_ts, self._next_allowed, self._cooldown_until)
            wait_for = ready_at - now_ts
            self._next_allowed = ready_at + self._interval
        if wait_for > 0:
            if stop:
                if stop.wait(wait_for):
                    self._sem.release()
                    return False
            else:
                time.sleep(wait_for)
        if stop and stop.is_set():
            self._sem.release()
            return False
        return True

    def release(self) -> None:
        self._sem.release()

    @contextmanager
    def slot(self, stop=None):
        acquired = self.acquire(stop=stop)
        try:
            yield acquired
        finally:
            if acquired:
                self.release()

    def note_success(self) -> None:
        with self._lock:
            self._successes += 1
            if self._successes >= 30:
                self._interval = max(self._base_interval, self._interval * 0.9)
                self._successes = 0

    def note_rate_limit(self, retry_after=None) -> None:
        delay = 8.0
        if retry_after is not None:
            try:
                parsed = float(retry_after)
                if parsed > 0:
                    delay = max(8.0, min(60.0, parsed))
            except (TypeError, ValueError):
                pass
        with self._lock:
            now_ts = time.monotonic()
            self._cooldown_until = max(self._cooldown_until, now_ts + delay)
            self._next_allowed = max(self._next_allowed, self._cooldown_until)
            self._interval = min(0.65, self._interval * 1.35 + 0.05)
            self._successes = 0


def _valid_catalog_record(value, *, max_age=None, require_audit: bool = False) -> bool:
    if (
        not isinstance(value, dict)
        or value.get("developerId") != GOOGLE_DEVELOPER_ID
        or not isinstance(value.get("apps"), dict)
    ):
        return False
    apps = value["apps"]
    if not apps:
        return False
    if any(
        not isinstance(k, str)
        or not re.fullmatch(r"\d+", k)
        or not isinstance(v, str)
        or not v.strip()
        for k, v in apps.items()
    ):
        return False
    if require_audit:
        iap_apps = value.get("iapApps")
        no_iap_apps = value.get("noIapApps")
        if (
            not isinstance(iap_apps, dict)
            or len(iap_apps) < len(APPLE_APPS)
            or not isinstance(no_iap_apps, dict)
        ):
            return False
    if max_age is not None:
        try:
            fetched = datetime.fromisoformat(value.get("discoveredAt") or "")
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if not fetched.tzinfo or not (0 <= age <= max_age):
                return False
        except (TypeError, ValueError):
            return False
    return True


def order_app_map(apps: dict) -> dict[str, str]:
    priority = {app_id: idx for idx, app_id in enumerate(PRIORITY_APP_IDS)}
    items = [
        (str(k), str(v).strip())
        for k, v in apps.items()
        if re.fullmatch(r"\d+", str(k)) and isinstance(v, str) and v.strip()
    ]
    items.sort(key=lambda pair: (0 if pair[0] in priority else 1, priority.get(pair[0], 0), int(pair[0])))
    return dict(items)


def load_persisted_catalog(catalog_path: Path | None = None):
    target = catalog_path or CATALOG_PATH
    data = read_json(target)
    return data if _valid_catalog_record(data) else None


def allowed_apple_apps(snapshot=None, *, catalog_path: Path | None = None) -> dict[str, str]:
    merged = dict(APPLE_APPS)
    persisted = load_persisted_catalog(catalog_path)
    if persisted:
        merged.update(persisted["apps"])
    if isinstance(snapshot, dict):
        cat = snapshot.get("appleCatalog")
        if _valid_catalog_record(cat):
            merged.update(cat["apps"])
    return merged


def count_iap_pairs(page_html: str) -> int:
    pairs = 0
    for match in re.finditer(r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if (
                    node.get("$kind") == "textPair"
                    and isinstance(node.get("leadingText"), str)
                    and isinstance(node.get("trailingText"), str)
                ):
                    pairs += 1
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    if pairs == 0:
        pairs += len(
            re.findall(
                r'<div\b[^>]*class=["\'][^"\']*text-pair[^"\']*["\'][^>]*>\s*<span\b[^>]*>.*?</span>\s*<span\b[^>]*>.*?</span>',
                page_html,
                re.S | re.I,
            )
        )
    return pairs


def _audit_all_google_apps(ordered, origin_storefront, *, emit=lambda m: None, stop=None):
    emit(f"Apple 开发者核验：正在逐一进入 {len(ordered)} 个 Google 官方软件检查内购/订阅列表")
    limiter = AppleRateLimiter(max_concurrency=4, min_interval=0.22)
    tls = threading.local()
    sessions = []
    sessions_lock = threading.Lock()

    def get_session():
        sess = getattr(tls, "session", None)
        if sess is None:
            sess = requests.Session(impersonate="chrome", trust_env=False)
            tls.session = sess
            with sessions_lock:
                sessions.append(sess)
        return sess

    def inspect_one(entry):
        app_id, app_name = entry
        if stop and stop.is_set():
            return app_id, app_name, "error", None
        primary_sf = origin_storefront.get(app_id, "us")
        candidates = [primary_sf] if primary_sf == "us" else [primary_sf, "us"]
        sess = get_session()
        headers = {"Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache"}
        for sf in candidates:
            url = f"https://apps.apple.com/{sf}/app/id{app_id}"
            for _ in range(1, 4):
                with limiter.slot(stop=stop) as ok:
                    if not ok:
                        return app_id, app_name, "error", None
                    try:
                        resp = sess.get(url, timeout=12, allow_redirects=True, headers=headers)
                    except Exception:
                        resp = None
                if resp is None:
                    continue
                if resp.status_code in {429, 503}:
                    retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                    limiter.note_rate_limit(retry_after)
                    continue
                if resp.status_code == 404:
                    limiter.note_success()
                    break
                if resp.status_code == 200:
                    limiter.note_success()
                    pair_count = count_iap_pairs(resp.text)
                    final_sf = (urlparse(resp.url).path.strip("/").split("/", 1)[0] or sf).upper()
                    if pair_count > 0 or app_id in APPLE_APPS:
                        return app_id, app_name, "iap", None
                    return (
                        app_id,
                        app_name,
                        "no_iap",
                        {
                            "appId": app_id,
                            "appName": app_name,
                            "checkedStorefront": final_sf,
                            "sourceUrl": resp.url,
                            "checkedAt": now_iso(),
                            "hasInAppPurchases": False,
                            "reason": "已进入该 Google 官方软件商店页核验：无任何 App 内购买/订阅项",
                        },
                    )
                break
        return app_id, app_name, "iap" if app_id in APPLE_APPS else "unknown", None

    iap_apps = {}
    no_iap_apps = {}
    try:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="apple-audit") as pool:
            for app_id, app_name, kind, meta in pool.map(inspect_one, ordered.items()):
                if kind == "no_iap" and meta is not None and app_id not in APPLE_APPS:
                    no_iap_apps[app_id] = meta
                else:
                    iap_apps[app_id] = app_name
    finally:
        for sess in sessions:
            try:
                sess.close()
            except Exception:
                pass

    for app_id, app_name in APPLE_APPS.items():
        no_iap_apps.pop(app_id, None)
        iap_apps.setdefault(app_id, app_name)

    return order_app_map(iap_apps), {aid: no_iap_apps[aid] for aid in ordered if aid in no_iap_apps}


def discover_google_apps(
    emit=lambda message: None,
    *,
    force: bool = False,
    stop=None,
    max_age: int = 86400,
    audit_iaps: bool = True,
    catalog_path: Path | None = None,
):
    """Discover all Google (artistId=281956209) apps and audit IAP presence."""
    global _CATALOG
    target_path = catalog_path or CATALOG_PATH
    with _CATALOG_LOCK:
        if not force and _valid_catalog_record(_CATALOG, max_age=max_age, require_audit=audit_iaps):
            return copy.deepcopy(_CATALOG)
        if not force:
            persisted = load_persisted_catalog(target_path)
            if _valid_catalog_record(persisted, max_age=max_age, require_audit=audit_iaps):
                _CATALOG = persisted
                return copy.deepcopy(persisted)

        emit("Apple 开发者目录：正在拉取 Google（ID 281956209）全部上架软件列表")
        discovered = {}
        origin_storefront = {}
        used_seeds = []
        try:
            with requests.Session(impersonate="chrome", trust_env=False) as session:
                for seed in SEED_STOREFRONTS:
                    if stop and stop.is_set():
                        break
                    url = f"https://itunes.apple.com/lookup?id={GOOGLE_DEVELOPER_ID}&entity=software&country={seed}&limit=200"
                    try:
                        resp = session.get(url, timeout=12, headers={"Accept": "application/json"})
                    except Exception:
                        continue
                    if resp.status_code in {403, 429}:
                        if len(discovered) >= 50:
                            break
                        if stop:
                            stop.wait(1.0)
                        else:
                            time.sleep(1.0)
                        continue
                    if resp.status_code != 200:
                        continue
                    try:
                        payload = resp.json()
                    except ValueError:
                        continue
                    results = payload.get("results", []) if isinstance(payload, dict) else []
                    found_any = False
                    for item in results:
                        if not isinstance(item, dict) or item.get("wrapperType") != "software":
                            continue
                        if str(item.get("artistId") or "") != GOOGLE_DEVELOPER_ID:
                            continue
                        tid = str(item.get("trackId") or "").strip()
                        name = str(item.get("trackName") or "").strip()
                        if not re.fullmatch(r"\d+", tid) or not name:
                            continue
                        found_any = True
                        if tid not in discovered:
                            discovered[tid] = name
                            origin_storefront[tid] = seed
                    if found_any:
                        used_seeds.append(seed)

                if not discovered and not (stop and stop.is_set()):
                    try:
                        dev_resp = session.get(
                            f"https://apps.apple.com/us/developer/google/id{GOOGLE_DEVELOPER_ID}",
                            timeout=12,
                            headers={"Accept-Language": "en-US,en;q=0.9"},
                        )
                        if dev_resp.status_code == 200:
                            for match in re.finditer(r"/app/(?:([^/\"'?#]+)/)?id(\d+)", dev_resp.text):
                                slug, tid = match.group(1), match.group(2)
                                if tid not in discovered:
                                    label = APPLE_APPS.get(tid) or (slug.replace("-", " ").title() if slug else f"Google App {tid}")
                                    discovered[tid] = label
                                    origin_storefront[tid] = "us"
                            if discovered:
                                used_seeds.append("us-developer-page")
                    except Exception:
                        pass
        except Exception:
            pass

        if not discovered:
            persisted = load_persisted_catalog(target_path)
            if persisted and (not audit_iaps or _valid_catalog_record(persisted, require_audit=True)):
                emit(f"Apple 开发者目录刷新未完成，沿用已保存的 {len(persisted['apps'])} 个 Google 软件")
                _CATALOG = persisted
                return copy.deepcopy(persisted)

        for app_id, name in APPLE_APPS.items():
            discovered.setdefault(app_id, name)
            origin_storefront.setdefault(app_id, "us")

        ordered = order_app_map(discovered)
        if audit_iaps and not (stop and stop.is_set()):
            iap_apps, no_iap_apps = _audit_all_google_apps(ordered, origin_storefront, emit=emit, stop=stop)
        else:
            iap_apps, no_iap_apps = dict(ordered), {}

        record = {
            "developerId": GOOGLE_DEVELOPER_ID,
            "developerName": GOOGLE_DEVELOPER_NAME,
            "sellerName": GOOGLE_SELLER_NAME,
            "discoveredAt": now_iso(),
            "seedStorefronts": used_seeds or list(SEED_STOREFRONTS[:1]),
            "appCount": len(ordered),
            "iapAppCount": len(iap_apps),
            "noIapAppCount": len(no_iap_apps),
            "apps": ordered,
            "iapApps": iap_apps,
            "noIapApps": no_iap_apps,
        }
        if not (stop and stop.is_set()):
            write_json(target_path, record)
            _CATALOG = record
            if audit_iaps:
                emit(
                    f"Apple 开发者目录核验完成：共 {len(ordered)} 个 Google 软件，逐一进入检查后确认 "
                    f"{len(iap_apps)} 个含内购/订阅（将逐区全量采集），{len(no_iap_apps)} 个无任何内购项"
                )
            else:
                emit(f"Apple 开发者目录：已发现 {len(ordered)} 个 Google 官方软件（不重复、不遗漏）")
        return copy.deepcopy(record)
