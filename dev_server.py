#!/usr/bin/env python3
"""Lightweight loopback QA & preview server for public/ and /api/* routes.

Used by automated Playwright UI tests (tests/verify_ui.py) and local preview
(npm run dev). Never writes outside public/data/overrides.json or starts a LaunchAgent.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import mimetypes
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from collector.common import DATA_DIR, fetch_bank_of_italy_fx, now_iso, read_json, write_json
from collector.publisher import OVERRIDES_PATH, empty_overrides, load_overrides

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
COOKIE_NAME = "ai_prices_admin"


def _signing_key(secret: str) -> bytes:
    return hashlib.sha256(f"ai-subscription-prices-cookie:{secret}".encode("utf-8")).digest()


def _make_token(password: str) -> str:
    payload = {"v": 1, "role": "admin", "iat": int(time.time()), "exp": int(time.time()) + 43200}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_signing_key(password), body.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{body}.{sig_b64}"


def _verify_token(token: str, password: str) -> bool:
    if not token or "." not in token or not password:
        return False
    try:
        body, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(_signing_key(password), body.encode("ascii"), hashlib.sha256).digest()
        padded = sig_b64 + "=" * ((4 - len(sig_b64) % 4) % 4)
        actual_sig = base64.urlsafe_b64decode(padded.encode("ascii"))
        if not hmac.compare_digest(expected_sig, actual_sig):
            return False
        body_padded = body + "=" * ((4 - len(body) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body_padded.encode("ascii")).decode("utf-8"))
        return payload.get("role") == "admin" and int(payload.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def make_handler(admin_password: str, overrides_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def _send_json(self, payload: dict, status: int = 200, extra_headers: dict | None = None):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def _is_admin(self) -> bool:
            cookie_header = self.headers.get("Cookie", "")
            cookie = SimpleCookie()
            cookie.load(cookie_header)
            morsel = cookie.get(COOKIE_NAME)
            return bool(morsel and _verify_token(morsel.value, admin_password))

        def _read_json(self) -> dict | None:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 65536:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return None

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/api/health":
                return self._send_json({
                    "ok": True,
                    "service": "ai-subscription-prices-local-preview",
                    "adminConfigured": bool(admin_password),
                    "githubConfigured": False,
                    "now": now_iso(),
                })

            if path == "/api/fx":
                fx = read_json(DATA_DIR / "fx.json") or fetch_bank_of_italy_fx()
                return self._send_json(fx)

            if path == "/api/admin/session":
                return self._send_json({
                    "authenticated": self._is_admin(),
                    "adminConfigured": bool(admin_password),
                    "githubConfigured": False,
                })

            if path == "/api/admin/status":
                if not self._is_admin():
                    return self._send_json({"ok": False, "error": "未登录管理员"}, 401)
                return self._send_json({
                    "ok": True,
                    "githubConfigured": False,
                    "repo": None,
                    "proxySecrets": {
                        "IPROYAL_PROXY_USER": {"configured": bool(os.environ.get("IPROYAL_PROXY_USER"))},
                        "IPROYAL_PROXY_PASS": {"configured": bool(os.environ.get("IPROYAL_PROXY_PASS"))},
                        "IPROYAL_PROXY_ENDPOINT": {"configured": bool(os.environ.get("IPROYAL_PROXY_ENDPOINT"))},
                    },
                })

            if path == "/api/admin/jobs":
                if not self._is_admin():
                    return self._send_json({"ok": False, "error": "未登录管理员"}, 401)
                return self._send_json({"ok": True, "githubConfigured": False, "runs": []})

            # Serve static files under public/
            rel = "index.html" if path == "/" else path.lstrip("/")
            if path == "/data/overrides.json":
                doc = load_overrides(overrides_path)
                return self._send_json(doc)

            target = (PUBLIC_DIR / rel).resolve()
            if PUBLIC_DIR.resolve() not in target.parents and target != PUBLIC_DIR.resolve():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            ctype, _ = mimetypes.guess_type(str(target))
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype or 'application/octet-stream'}; charset=utf-8" if (ctype or "").startswith(("text/", "application/javascript", "application/json")) else (ctype or "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/api/admin/login":
                body = self._read_json()
                pw = (body or {}).get("password")
                if not admin_password:
                    return self._send_json({"ok": False, "error": "未设置 ADMIN_PASSWORD"}, 503)
                if not isinstance(pw, str) or not hmac.compare_digest(
                    hashlib.sha256(pw.encode("utf-8")).digest(),
                    hashlib.sha256(admin_password.encode("utf-8")).digest(),
                ):
                    return self._send_json({"ok": False, "error": "管理员口令不正确"}, 401)
                token = _make_token(admin_password)
                return self._send_json(
                    {"ok": True, "authenticated": True},
                    200,
                    {"Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"},
                )

            if path == "/api/admin/logout":
                return self._send_json(
                    {"ok": True, "authenticated": False},
                    200,
                    {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
                )

            if path == "/api/admin/overrides":
                if not self._is_admin():
                    return self._send_json({"ok": False, "error": "未登录管理员"}, 401)
                body = self._read_json() or {}
                product = str(body.get("product") or "")
                country = str(body.get("country") or "").upper()
                channel = str(body.get("channel") or "")
                plan = str(body.get("plan") or "")
                currency = str(body.get("currency") or "").upper()
                amount = body.get("amount")
                if product not in {"chatgpt", "google_ultra"} or len(country) != 2 or channel not in {"web", "apple"} or not plan or len(currency) != 3 or not isinstance(amount, (int, float)) or amount <= 0:
                    return self._send_json({"ok": False, "error": "校准参数无效"}, 400)
                doc = load_overrides(overrides_path)
                key = f"{product}:{country}:{channel}:{plan}"
                doc.setdefault("entries", {})[key] = {
                    "product": product,
                    "country": country,
                    "channel": channel,
                    "plan": plan,
                    "currency": currency,
                    "amount": float(amount),
                    "note": str(body.get("note") or "")[:200],
                    "updatedAt": now_iso(),
                }
                doc["updatedAt"] = now_iso()
                write_json(overrides_path, doc)
                return self._send_json({"ok": True, "key": key, "overrides": doc})

            if path == "/api/admin/refresh":
                if not self._is_admin():
                    return self._send_json({"ok": False, "error": "未登录管理员"}, 401)
                return self._send_json({"ok": False, "error": "本地预览服务不连接 GitHub；部署至云端并配置 GITHUB_TOKEN 后可一键派发"}, 503)

            return self._send_json({"ok": False, "error": "Not found"}, 404)

        def do_DELETE(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/admin/overrides":
                return self._send_json({"ok": False, "error": "Not found"}, 404)
            if not self._is_admin():
                return self._send_json({"ok": False, "error": "未登录管理员"}, 401)
            body = self._read_json() or {}
            key = f"{body.get('product')}:{str(body.get('country') or '').upper()}:{body.get('channel')}:{body.get('plan')}"
            doc = load_overrides(overrides_path)
            doc.setdefault("entries", {}).pop(key, None)
            doc["updatedAt"] = now_iso()
            write_json(overrides_path, doc)
            return self._send_json({"ok": True, "key": key, "overrides": doc})

    return Handler


def start_server(port: int = 18780, *, admin_password: str | None = None, overrides_path: Path | None = None) -> ThreadingHTTPServer:
    pw = admin_password if admin_password is not None else os.environ.get("ADMIN_PASSWORD", "preview-admin")
    ov_path = overrides_path or OVERRIDES_PATH
    if not ov_path.exists():
        write_json(ov_path, empty_overrides())
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(pw, ov_path))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18780)
    args = parser.parse_args()
    srv = start_server(args.port)
    print(f"Preview server listening at http://127.0.0.1:{args.port}/", flush=True)
    srv.serve_forever()
