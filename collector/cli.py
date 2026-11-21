"""Cloud & GitHub Actions CLI entrypoint for daily Apple patrols and web refreshes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import sys
import threading

from collector import chatgpt_apple, chatgpt_web, google_apple, google_web
from collector.common import (
    DATA_DIR,
    fetch_bank_of_italy_fx,
    now_iso,
    proxy_config_from_env,
    read_json,
    redact_secrets,
)
from collector.google_apps import AppleRateLimiter, discover_google_apps
from collector.history_ledger import load_ledger, save_ledger
from collector.publisher import (
    LEDGER_PATH,
    OVERRIDES_PATH,
    PUBLISHED_PATH,
    build_published_snapshot,
    load_overrides,
    save_published,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ChatGPT & Google AI Ultra global monthly prices.")
    parser.add_argument("--product", choices=("all", "chatgpt", "google_ultra"), default="all")
    parser.add_argument("--scope", choices=("apple", "web", "all"), default="apple")
    parser.add_argument("--mode", choices=("full", "retry"), default="full")
    parser.add_argument("--countries", default="", help="Comma-separated ISO alpha-2 country codes")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-fx", action="store_true")
    parser.add_argument("--discover-apps", action="store_true", help="Force re-discovery of Google LLC apps")
    return parser.parse_args(argv)


def _needs_retry(channel_obj: dict, plans: tuple[str, ...]) -> bool:
    if not isinstance(channel_obj, dict):
        return True
    if channel_obj.get("status") in {"unavailable"} and channel_obj.get("availabilityConfirmed"):
        return False
    cells = channel_obj.get("cells") or {}
    return any((cells.get(p) or {}).get("status") not in {"ok", "unavailable"} for p in plans)


def run_collection(args: argparse.Namespace) -> int:
    existing = read_json(PUBLISHED_PATH)
    if not isinstance(existing, dict) or not isinstance(existing.get("products"), dict):
        raise SystemExit("Missing public/data/published.json. Run python3 -m collector.migrate_existing first.")

    secrets_tuple: tuple[str, ...] = ()
    proxy_cfg = None
    if args.scope in {"web", "all"}:
        proxy_cfg = proxy_config_from_env()
        if not proxy_cfg:
            raise SystemExit("Missing IPROYAL_PROXY_USER / IPROYAL_PROXY_PASS / IPROYAL_PROXY_ENDPOINT for web refresh.")
        secrets_tuple = (proxy_cfg["proxyUser"], proxy_cfg["proxyPass"], proxy_cfg["proxyEndpoint"])

    log_lock = threading.Lock()

    def emit(message: str) -> None:
        with log_lock:
            print(redact_secrets(message, secrets_tuple), flush=True)

    target_filter = {
        c.strip().upper()
        for c in str(args.countries or "").split(",")
        if c.strip()
    }

    products_out = {
        "chatgpt": copy.deepcopy(existing["products"]["chatgpt"]),
        "google_ultra": copy.deepcopy(existing["products"]["google_ultra"]),
    }

    do_chatgpt = args.product in {"all", "chatgpt"}
    do_google = args.product in {"all", "google_ultra"}
    do_apple = args.scope in {"apple", "all"}
    do_web = args.scope in {"web", "all"}

    if args.skip_fx:
        fx = existing.get("fx") or read_json(DATA_DIR / "fx.json") or {"rates": {"EUR": {"rate": 1.0, "date": None}}}
    else:
        emit("正在更新意大利央行欧元汇率...")
        fx = fetch_bank_of_italy_fx()

    # Preflight proxy once if running web
    google_transport = None
    chatgpt_transport = None
    if do_web and proxy_cfg:
        emit("正在对住宅代理网关执行出站预检...")
        spec, detail = google_web.preflight(proxy_cfg, emit=emit)
        if spec is None:
            raise SystemExit(f"Residential proxy preflight failed: {detail.get('reason')}")
        google_transport = spec
        chatgpt_transport = spec

    # Google AI Ultra Web
    if do_google and do_web and google_transport:
        gu_rows = products_out["google_ultra"]["countries"]
        policy = google_web.verify_policy(emit=emit)
        targets = [
            row for row in gu_rows
            if (not target_filter or row["code"] in target_filter)
            and (args.mode == "full" or _needs_retry(row.get("web"), google_web.PLANS))
        ]
        emit(f"Google AI Ultra 官网抓取开始：共 {len(targets)} 个目标地区")

        def run_gu_web(row):
            res = google_web.fetch_country(row["code"], proxy_cfg, google_transport, emit=emit, policy=policy)
            proxy_val = res.pop("proxy", None)
            if proxy_val is not None:
                row["proxy"] = proxy_val
            row["web"] = res

        with ThreadPoolExecutor(max_workers=max(1, min(6, args.workers))) as pool:
            list(pool.map(run_gu_web, targets))
        products_out["google_ultra"]["webUpdatedAt"] = now_iso()

    # ChatGPT Web
    if do_chatgpt and do_web and chatgpt_transport:
        cg_rows = products_out["chatgpt"]["countries"]
        targets = [
            row for row in cg_rows
            if row.get("inProxyCatalog", True)
            and (not target_filter or row["code"] in target_filter)
            and (args.mode == "full" or _needs_retry(row.get("web"), chatgpt_web.PLANS))
        ]
        emit(f"ChatGPT 官网抓取开始：共 {len(targets)} 个目标地区")

        def run_cg_web(row):
            res = chatgpt_web.fetch_country(row["code"], proxy_cfg, chatgpt_transport, emit=emit)
            proxy_val = res.pop("proxy", None)
            if proxy_val is not None:
                row["proxy"] = proxy_val
            row["web"] = res

        with ThreadPoolExecutor(max_workers=max(1, min(6, args.workers))) as pool:
            list(pool.map(run_cg_web, targets))
        products_out["chatgpt"]["webUpdatedAt"] = now_iso()

    # Apple direct patrols (zero residential proxy traffic)
    limiter = AppleRateLimiter(max_concurrency=4, min_interval=0.25)

    if do_google and do_apple:
        gu_rows = products_out["google_ultra"]["countries"]
        catalog = discover_google_apps(emit=emit, force=args.discover_apps, audit_iaps=True)
        products_out["google_ultra"]["appleCatalog"] = catalog
        policy = google_apple.verify_policy(emit=emit)
        targets = [
            row for row in gu_rows
            if (not target_filter or row["code"] in target_filter)
            and (args.mode == "full" or _needs_retry(row.get("apple"), google_apple.PLANS))
        ]
        emit(f"Google AI Ultra App Store 巡检开始：共 {len(targets)} 个地区（{catalog.get('iapAppCount', 16)} 款含内购软件）")

        def run_gu_apple(row):
            row["apple"] = google_apple.fetch_country(
                row["code"], emit=emit, policy=policy, catalog=catalog, limiter=limiter
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run_gu_apple, targets))
        products_out["google_ultra"]["appleUpdatedAt"] = now_iso()

    if do_chatgpt and do_apple:
        cg_rows = products_out["chatgpt"]["countries"]
        targets = [
            row for row in cg_rows
            if (not target_filter or row["code"] in target_filter)
            and (args.mode == "full" or _needs_retry(row.get("apple"), chatgpt_apple.PLANS))
        ]
        emit(f"ChatGPT App Store 巡检开始：共 {len(targets)} 个商店区")

        def run_cg_apple(row):
            row["apple"] = chatgpt_apple.fetch_country(row["code"], emit=emit, limiter=limiter)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(run_cg_apple, targets))
        products_out["chatgpt"]["appleUpdatedAt"] = now_iso()

    ledger = load_ledger(LEDGER_PATH)
    overrides = load_overrides(OVERRIDES_PATH)
    update_ledger_for = set()
    if do_apple and do_chatgpt:
        update_ledger_for.add("chatgpt")
    if do_apple and do_google:
        update_ledger_for.add("google_ultra")

    snapshot = build_published_snapshot(
        products_out,
        ledger=ledger,
        overrides=overrides,
        fx=fx,
        update_apple_ledger_for=update_ledger_for,
    )
    save_ledger(LEDGER_PATH, ledger)
    save_published(snapshot, PUBLISHED_PATH)
    emit(
        f"发布完成：ChatGPT 有效 {snapshot['products']['chatgpt']['summary']['validRegions']} 区，"
        f"Google Ultra 有效 {snapshot['products']['google_ultra']['summary']['validRegions']} 区"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return run_collection(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
