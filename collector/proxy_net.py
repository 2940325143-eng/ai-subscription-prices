"""Shared, credential-safe IPRoyal sticky residential proxy transport helpers."""
from __future__ import annotations

import ipaddress
import re
import secrets
import socket
import string
import time
from urllib.parse import urlparse

from curl_cffi import CurlOpt, requests

GEO_URL = "https://api.country.is/"
BACKUP_GEO_URL = "https://get.geojs.io/v1/ip/country.json"
KNOWN_IP = "23.146.144.102"
TLS_HOST = "us.proxy.iproyal.com"
SESSION_ALPHABET = string.ascii_letters + string.digits


def new_session_id() -> str:
    return "".join(secrets.choice(SESSION_ALPHABET) for _ in range(8))


def country_password(password: str, country: str, session_id: str | None = None, lifetime: str = "10m") -> str:
    if not re.fullmatch(r"[A-Z]{2}", str(country)):
        raise ValueError("Invalid proxy country")
    value = re.sub(r"_session-[A-Za-z0-9]+|_lifetime-\d+[smhd]|_forcerandom-[01]", "", password, flags=re.I)
    suffix = "_country-" + country.lower()
    value = (
        re.sub(r"_country-[a-z]{2}(?:,[a-z]{2})*", suffix, value, flags=re.I)
        if re.search(r"_country-", value, re.I)
        else value + suffix
    )
    if session_id:
        if not re.fullmatch(r"[a-zA-Z0-9]{8}", session_id):
            raise ValueError("Invalid sticky session")
        value += f"_session-{session_id}_lifetime-{lifetime}"
    return value


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict) or set(spec) - {"proxyServer", "proxyResolve", "proxyInterface"}:
        raise ValueError("Invalid transport")
    parsed = urlparse(spec.get("proxyServer", ""))
    try:
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid proxy gateway")
    except ValueError:
        raise ValueError("Invalid proxy gateway") from None
    result: dict = {"proxyServer": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    interface = spec.get("proxyInterface")
    if interface:
        if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", interface):
            raise ValueError("Invalid network interface")
        result["proxyInterface"] = interface
    resolve = spec.get("proxyResolve")
    if resolve:
        if not isinstance(resolve, list) or len(resolve) > 4:
            raise ValueError("Invalid proxy resolution")
        result["proxyResolve"] = []
        for entry in resolve:
            if not isinstance(entry, str) or not entry.startswith(f"{parsed.hostname}:{parsed.port}:"):
                raise ValueError("Mismatched proxy resolution")
            address = str(ipaddress.IPv4Address(entry.rsplit(":", 1)[1]))
            result["proxyResolve"].append(f"{parsed.hostname}:{parsed.port}:{address}")
    return result


def transport_candidates(endpoint: str) -> list[dict]:
    base = validate_spec({"proxyServer": endpoint if "://" in endpoint else "http://" + endpoint})
    parsed = urlparse(base["proxyServer"])
    try:
        available = {name for _, name in socket.if_nameindex()}
    except OSError:
        available = set()
    interfaces = [name for name in ("en1", "en0") if name in available]
    if parsed.hostname in {KNOWN_IP, TLS_HOST} and parsed.port == 12321:
        tls = {"proxyServer": f"https://{TLS_HOST}:12321", "proxyResolve": [f"{TLS_HOST}:12321:{KNOWN_IP}"]}
        http = {"proxyServer": f"http://{KNOWN_IP}:12321"}
        return (
            [tls]
            + [{**tls, "proxyInterface": name} for name in interfaces]
            + [{**http, "proxyInterface": name} for name in interfaces]
            + [http]
        )
    return [base] + [{**base, "proxyInterface": name} for name in interfaces]


def create_session(spec: dict) -> requests.Session:
    spec = validate_spec(spec)
    options = {CurlOpt.RESOLVE: spec["proxyResolve"]} if spec.get("proxyResolve") else {}
    return requests.Session(impersonate="chrome", trust_env=False, curl_options=options)


def proxy_kwargs(spec: dict, config: dict[str, str], country: str, sticky: str, *, lifetime: str = "10m") -> dict:
    spec = validate_spec(spec)
    value: dict = {
        "proxy": spec["proxyServer"],
        "proxy_auth": (config["proxyUser"], country_password(config["proxyPass"], country, sticky, lifetime=lifetime)),
    }
    if spec.get("proxyInterface"):
        value["interface"] = spec["proxyInterface"]
    return value


def safe_error(error: Exception) -> dict:
    """Never return raw exception text: curl can include proxy authentication."""
    result: dict = {"errorType": type(error).__name__, "reason": "request_failed"}
    code = getattr(error, "code", None)
    if type(code) is int:
        result["curlCode"] = code
        if code == 28:
            result["reason"] = "timeout"
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if type(status) is int:
        result["httpStatus"] = status
    if re.search(r"(?:CONNECT[^\r\n]{0,100}?response|HTTP(?:/[\d.]+)?)\s*[:=]?\s*407\b", str(error), re.I) or status == 407 or code == 67:
        result.update(reason="proxy_auth_required", httpStatus=407)
    return result


def verify_geo(session: requests.Session, kwargs: dict, country: str, timeout: float = 12) -> dict:
    last_error = None
    last_result = None
    per_try_timeout = min(10, max(1, timeout))
    for url, provider in ((GEO_URL, "country.is"), (BACKUP_GEO_URL, "geojs.io")):
        try:
            response = session.get(
                url,
                timeout=per_try_timeout,
                headers={"Accept": "application/json", "Cache-Control": "no-cache"},
                **kwargs,
            )
        except Exception as error:
            if safe_error(error).get("reason") == "proxy_auth_required":
                raise
            last_error = error
            continue
        if response.status_code == 407:
            return {
                "status": "error",
                "actualCountry": None,
                "httpStatus": 407,
                "reason": "proxy_auth_required",
                "geoProvider": provider,
            }
        if response.status_code != 200:
            last_result = {
                "status": "error",
                "actualCountry": None,
                "httpStatus": response.status_code,
                "reason": "geolocation_http_error",
                "geoProvider": provider,
            }
            last_error = None
            continue
        try:
            data = response.json()
            actual = data.get("country") if isinstance(data, dict) else None
            address = data.get("ip") if isinstance(data, dict) else None
            if not isinstance(actual, str) or not re.fullmatch(r"[A-Z]{2}", actual) or not isinstance(address, str):
                raise ValueError("Malformed geolocation response")
            ipaddress.ip_address(address)
        except Exception as error:
            last_error = error
            continue
        return {
            "status": "ok" if actual == country else "mismatch",
            "actualCountry": actual,
            "ip": address,
            "reason": "country_matched" if actual == country else "country_mismatch",
            "geoProvider": provider,
        }
    if last_error is not None:
        raise last_error
    return last_result or {"status": "error", "actualCountry": None, "reason": "geolocation_http_error"}


def preflight(config: dict[str, str], stop=None, emit=lambda message: None, budget: float = 40):
    deadline = time.monotonic() + min(40, max(0, budget))
    attempts = []
    if not all(config.get(key) for key in ("proxyUser", "proxyPass", "proxyEndpoint")):
        return None, {"status": "error", "reason": "missing_proxy_config", "attempts": attempts}
    try:
        candidates = transport_candidates(config["proxyEndpoint"])
    except (TypeError, ValueError):
        return None, {"status": "error", "reason": "invalid_proxy_config", "attempts": attempts}
    for index, spec in enumerate(candidates, 1):
        if stop and stop.is_set():
            return None, {"status": "cancelled", "reason": "cancelled", "attempts": attempts}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        emit(
            f"代理入口预检 {index}/{len(candidates)}：{spec['proxyServer'].split(':')[0]} / "
            f"{spec.get('proxyInterface', 'default')}，最多 {min(8, remaining):.0f}s"
        )
        try:
            with create_session(spec) as session:
                detail = verify_geo(session, proxy_kwargs(spec, config, "US", new_session_id()), "US", min(8, remaining))
        except Exception as error:
            detail = {"status": "error", **safe_error(error)}
        attempts.append({key: value for key, value in detail.items() if key != "ip"})
        if detail["status"] == "ok":
            emit("代理入口预检通过；逐国请求将重新核验同一会话出口")
            return spec, {"status": "ok", "reason": "country_matched", "attempts": attempts}
        emit(f"代理入口预检失败：{detail.get('reason', 'request_failed')}")
        if detail.get("reason") == "proxy_auth_required":
            break
    return None, {
        "status": "error",
        "reason": attempts[-1].get("reason", "gateway_unreachable") if attempts else "gateway_unreachable",
        "attempts": attempts,
    }
