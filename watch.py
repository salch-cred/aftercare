"""Finding what the agent said is live, checking it, and remembering how it went."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

# claims ------------------------------------------------------------------

_URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.I)
_LIVE = re.compile(r"""
    \b(?:deployed|redeployed|is\ live|now\ live|went\ live|live\ at|published|launched|shipped)\b
  | \b(?:is|are)\ (?:now\ )?(?:up|online|running|reachable|available|serving)\b
  | \bup\ and\ running\b | \b(?:available|hosted|running|served|reachable)\ (?:at|on)\b
  | \byou\ can\ (?:now\ )?(?:see|view|visit|open|try|reach)\ it\b
""", re.I | re.X)
_NOT = re.compile(r"\b(?:not|never|isn'?t|aren'?t|wasn'?t|failed|couldn'?t|can'?t|unable|if|once|when|after you)\b", re.I)


def _strip(url: str) -> str:
    return url.rstrip(".,;:!?*_")


def claims(text: str) -> list[tuple[str, str]]:
    """(url, sentence) for each public URL the reply says is deployed or live."""
    found: list[tuple[str, str]] = []
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    for line in text.splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            if "?" in sentence.rstrip()[-1:] or not _LIVE.search(sentence) or _NOT.search(sentence):
                continue
            for raw in _URL.findall(sentence):
                url = _strip(raw)
                if public(url) and url not in [u for u, _ in found]:
                    found.append((url, " ".join(sentence.split())[:200]))
    return found


# safety ------------------------------------------------------------------

def _private_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def public(url: str) -> bool:
    """Only http(s) addresses on the public internet: never localhost or a private network."""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    if _private_ip(host):
        return False
    return "." in host


def resolves_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True            # let the request itself report the DNS failure
    return not any(_private_ip(info[4][0]) for info in infos)


# probing ---------------------------------------------------------------

@dataclass
class Result:
    ok: bool
    status: Optional[int]
    detail: str
    ms: int


class _NoPrivateRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public(newurl):
            raise urllib.error.URLError(f"redirected to a non-public address ({newurl})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _tls() -> ssl.SSLContext:
    """Verified TLS. Some Python builds ship without a CA list, which would make every https
    site look down, so the certifi bundle is used when it is installed."""
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except Exception:
        pass
    return context


def probe(url: str, timeout: float = 8.0) -> Result:
    """One GET request; 2xx and 3xx count as up. Reads at most 1 KB of the body."""
    start = time.monotonic()
    ms = lambda: int((time.monotonic() - start) * 1000)
    if not public(url):
        return Result(False, None, "not a public address", 0)
    host = urlparse(url).hostname or ""
    if not resolves_public(host):
        return Result(False, None, "resolves to a private address, not checked", 0)
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-aftercare/0.1 (+uptime check)"})
    opener = urllib.request.build_opener(_NoPrivateRedirects, urllib.request.HTTPSHandler(context=_tls()))
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(1024)
            return Result(True, response.status, f"HTTP {response.status}", ms())
    except urllib.error.HTTPError as exc:
        return Result(exc.code < 400, exc.code, f"HTTP {exc.code}", ms())
    except urllib.error.URLError as exc:
        reason = exc.reason
        return Result(False, None, str(reason)[:120] if reason else "unreachable", ms())
    except (socket.timeout, TimeoutError):
        return Result(False, None, f"no answer within {timeout:g}s", ms())
    except Exception as exc:                       # a broken certificate, a reset connection
        return Result(False, None, f"{type(exc).__name__}: {exc}"[:120], ms())


# the record ------------------------------------------------------------

_LOCK = threading.Lock()


class Book:
    """Watched addresses in one JSON file: what was claimed, every check, and what to tell the user."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("watches"), list):
                data.setdefault("alerts", [])
                data.setdefault("next_id", 1)
                return data
        except Exception:
            pass
        return {"watches": [], "alerts": [], "next_id": 1}

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, fn: Callable[[dict], Any]) -> Any:
        with _LOCK:
            data = self.load()
            out = fn(data)
            self.save(data)
            return out

    def add(self, url: str, sentence: str, session_id: str, limit: int = 25) -> Optional[dict]:
        def go(data):
            for w in data["watches"]:
                if w["url"] == url:
                    w.update(sentence=sentence or w["sentence"], claimed=time.time(), session=session_id)
                    return None
            w = {"id": data["next_id"], "url": url, "sentence": sentence, "session": session_id,
                 "claimed": time.time(), "checked": 0, "ok": None, "detail": "", "history": []}
            data["next_id"] += 1
            data["watches"].append(w)
            del data["watches"][:-limit]
            return w
        return self.update(go)

    def forget(self, ident: str) -> bool:
        def go(data):
            before = len(data["watches"])
            data["watches"] = [w for w in data["watches"] if str(w["id"]) != ident and w["url"] != ident]
            return len(data["watches"]) < before
        return self.update(go)

    def expire(self, keep_days: int) -> None:
        cutoff = time.time() - keep_days * 86400
        self.update(lambda data: data.__setitem__("watches", [w for w in data["watches"] if w["claimed"] >= cutoff]))

    def due(self, every_hours: float) -> list[dict]:
        cutoff = time.time() - every_hours * 3600
        return [w for w in self.load()["watches"] if w["checked"] <= cutoff]

    def record(self, ident: int, result: Result) -> Optional[dict]:
        """Store a check. Returns an alert when the state changed in a way the user should hear."""
        def go(data):
            for w in data["watches"]:
                if w["id"] != ident:
                    continue
                before = w["ok"]
                w.update(checked=time.time(), ok=result.ok, detail=result.detail)
                w["history"] = (w["history"] + [[int(time.time()), result.ok, result.detail]])[-6:]
                kind = None
                if not result.ok and before is not False:
                    kind = "never_up" if before is None else "down"
                elif result.ok and before is False:
                    kind = "back"
                if kind:
                    alert = {"kind": kind, "url": w["url"], "sentence": w["sentence"], "claimed": w["claimed"],
                             "detail": result.detail, "at": time.time(), "told": False}
                    data["alerts"] = (data["alerts"] + [alert])[-20:]
                    return alert
            return None
        return self.update(go)

    def untold(self) -> list[dict]:
        def go(data):
            fresh = [a for a in data["alerts"] if not a["told"]]
            for a in data["alerts"]:
                a["told"] = True
            return fresh
        return self.update(go)


def run_checks(book: Book, watches: list[dict], timeout: float, check: Callable[[str, float], Result] = probe) -> list[tuple[dict, Result, Optional[dict]]]:
    out = []
    for w in watches:
        result = check(w["url"], timeout)
        out.append((w, result, book.record(w["id"], result)))
    return out
