#!/usr/bin/env python3
"""Playwright browser UI & responsive verification across desktop, 903px, and mobile."""
from __future__ import annotations

import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent / ".pricing-python"))
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(ROOT))
from dev_server import start_server


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    port = free_port()
    tmpdir = Path(tempfile.mkdtemp(prefix="ai-prices-ui-"))
    ov_path = tmpdir / "overrides.json"
    srv = start_server(port, admin_password="qa-password-123", overrides_path=ov_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    console_errors = []
    page_errors = []

    try:
        with sync_playwright() as p:
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            launch_opts = {"headless": True}
            if Path(chrome_path).exists():
                launch_opts["executable_path"] = chrome_path
            browser = p.chromium.launch(**launch_opts)
            for vp in (
                {"width": 1440, "height": 900},
                {"width": 903, "height": 900},
                {"width": 390, "height": 844},
            ):
                page = browser.new_page(viewport=vp)
                page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: page_errors.append(str(e)))

                page.goto(f"{base_url}/#chatgpt", wait_until="networkidle")
                page.wait_for_selector("tbody tr td.country-cell")

                overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not overflow, f"ChatGPT viewport {vp} horizontal overflow detected"

                # Check all 7 tiers visible in ChatGPT tab header
                header_text = page.locator("#tableHead").inner_text()
                for label in ("ChatGPT GO", "ChatGPT Plus", "Business Std", "Pro 100", "Business Prem", "Pro 200", "Pro 500"):
                    assert label in header_text, f"Missing ChatGPT tier {label} in header at {vp}"

                # Switch to team filter -> only 2 columns + rank/country/proxy
                page.get_by_role("button", name="团队 2 档").click()
                assert page.locator("#tableHead th.channel-head").count() == 2

                # Restore all 7 tiers
                page.get_by_role("button", name="全部 7 档").click()
                assert page.locator("#tableHead th.channel-head").count() == 12

                # Switch to Google AI Ultra tab
                page.locator("#tabGoogleUltra").click()
                page.wait_for_selector("text=Ultra 5× (20 TB)")
                overflow_gu = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not overflow_gu, f"Google AI Ultra viewport {vp} horizontal overflow detected"

                # Verify Google Ultra minimum cards do NOT pick Turkey's grandfathered TRY 1,989.99 / 2,949.99
                min_text = page.locator("#minimumGrid").inner_text()
                assert "1,989.99" not in min_text, f"Minimum card leaked TR 1,989.99 at {vp}"
                assert "2,949.99" not in min_text, f"Minimum card leaked TR 2,949.99 at {vp}"

                if vp["width"] == 1440:
                    # Search TR and verify amber suspect badge & modal
                    page.locator("#searchInput").fill("土耳其")
                    row_text = page.locator("#tableBody").inner_text()
                    assert "疑似旧续订价" in row_text, "TR row missing amber suspect badge"

                    # Open TR ultra5x Apple detail modal
                    page.locator('button.price-button[data-country="TR"][data-channel="apple"][data-plan="ultra5x"]').click()
                    detail_text = page.locator("#detailBody").inner_text()
                    assert "1,989.99" in detail_text
                    assert "App Store 历史价格轨迹" in detail_text
                    page.locator("#closeDetailsBtn").click()

                    # Test Admin wrong password -> toast error, right password -> unlock & override save/delete
                    page.locator("#openAdminBtn").click()
                    page.locator("#adminPasswordInput").fill("wrong-pass")
                    page.get_by_role("button", name="登录管理员").click()
                    page.wait_for_selector("#toast:not([hidden])")
                    assert "不正确" in page.locator("#toast").inner_text()

                    page.locator("#adminPasswordInput").fill("qa-password-123")
                    page.get_by_role("button", name="登录管理员").click()
                    page.wait_for_selector("#dispatchRefreshForm")
                    page.locator("#closeAdminBtn").click()

                    # Now open TR ultra5x Apple detail modal as admin and save + clear an override
                    page.locator('button.price-button[data-country="TR"][data-channel="apple"][data-plan="ultra5x"]').click()
                    page.wait_for_selector("#cellOverrideForm")
                    page.locator('#cellOverrideForm input[name="currency"]').fill("TRY")
                    page.locator('#cellOverrideForm input[name="amount"]').fill("4749.99")
                    page.locator('#cellOverrideForm input[name="note"]').fill("QA iOS live price")
                    page.get_by_role("button", name="保存实测校准价").click()
                    page.wait_for_selector("#deleteCellOverrideBtn")
                    assert "实测校准" in page.locator("#tableBody").inner_text()
                    page.locator("#deleteCellOverrideBtn").click()
                    page.wait_for_selector("#cellOverrideForm")
                    page.locator("#closeDetailsBtn").click()

                page.close()
            browser.close()
    finally:
        srv.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Filter expected intentional 401 console error from wrong password test
    unexpected_console = [m for m in console_errors if "401" not in m]
    assert not unexpected_console, f"Unexpected console errors: {unexpected_console}"
    assert not page_errors, f"Uncaught page errors: {page_errors}"
    print("PLAYWRIGHT QA PASSED: 1440px, 903px, 390px viewports, ChatGPT 7-tier, Google Ultra 2-tier, TR guard, modal & admin flow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
