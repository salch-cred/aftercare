"""aftercare: your agent checks its own work after the fact.

When a reply says something is live ("Deployed. The shop is live at https://shop.example.com"),
Aftercare remembers that address. Every few hours, at the start of a session, it checks each one
again with one plain GET request. If something the agent said was working stops answering,
the agent is told at the start of your next turn and tells you first. You hear when it is back,
too.

  /aftercare                what is watched, and how each one looked last time
  /aftercare check          check everything now
  /aftercare watch <url>    watch an address yourself
  /aftercare forget <n|url> stop watching one
  hermes aftercare [check]  the same from your shell, so it can run from cron
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import watch as W

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

_CTX: Any = None
_RUNNING = threading.Lock()          # one background round of checks at a time


def _setting(name: str, default: Any) -> Any:
    try:
        value = _CTX.get_config(name, default) if _CTX is not None else default
    except Exception:
        value = default
    return default if value is None or value == "" else value


def _num(name: str, default: float) -> float:
    try:
        return float(_setting(name, default))
    except (TypeError, ValueError):
        return default


def _flag(name: str, default: bool) -> bool:
    value = _setting(name, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("aftercare")
    except Exception:
        from hermes_constants import get_hermes_home
        path = get_hermes_home() / "plugin-data" / "aftercare"
        path.mkdir(parents=True, exist_ok=True)
        return path


def book() -> W.Book:
    return W.Book(_data_dir() / "aftercare.json")


def _text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(p.get("text", "") for p in message if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _day(ts: float) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(ts))


# hooks ------------------------------------------------------------------

def on_post_llm_call(session_id: str = "", assistant_response: Any = None, **_: Any) -> None:
    if not _flag("auto_watch", True):
        return None
    try:
        b = book()
        for url, sentence in W.claims(_text(assistant_response)):
            b.add(url, sentence, session_id or "", limit=int(_num("max_watches", 25)))
    except Exception:
        pass
    return None


def check_due(wait: bool = False, check=None) -> Optional[threading.Thread]:
    """Check what is due, in the background so a session never waits on the network."""
    b = book()
    b.expire(int(_num("keep_days", 30)))
    due = b.due(_num("every_hours", 6))
    if not due or not _RUNNING.acquire(blocking=False):
        return None

    def go():
        try:
            W.run_checks(b, due, _num("timeout", 8), check or W.probe)
        finally:
            _RUNNING.release()
    thread = threading.Thread(target=go, name="aftercare-checks", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return thread


def on_session_start(**_: Any) -> None:
    try:
        check_due()
    except Exception:
        pass
    return None


def alert_text(alerts: list[dict]) -> str:
    lines = []
    for a in alerts:
        when = _day(a["claimed"])
        if a["kind"] == "back":
            lines.append(f"- {a['url']} is answering again ({a['detail']}).")
        else:
            what = "has not worked since" if a["kind"] == "never_up" else "stopped working after"
            lines.append(f"- {a['url']} {what} you said on {when}: \"{a['sentence']}\" Now: {a['detail']}.")
    return lines and "\n".join(lines) or ""


def on_pre_llm_call(**_: Any) -> Optional[dict]:
    try:
        alerts = book().untold()
    except Exception:
        return None
    if not alerts:
        return None
    return {"context": "[Aftercare] Something you told the user was working has changed since:\n"
                       + alert_text(alerts)
                       + "\nTell the user this in one or two sentences at the start of your reply, "
                         "then continue with what they asked."}


# command and CLI ----------------------------------------------------------

def _status_line(w: dict) -> str:
    mark = {True: "✅", False: "❌", None: "⏳"}[w["ok"]]
    last = f"{w['detail']} at {_day(w['checked'])}" if w["checked"] else "not checked yet"
    return f"  {w['id']:>3}. {mark} {w['url']}  ({last})\n       claimed {_day(w['claimed'])}: \"{w['sentence'][:110]}\""


def listing() -> str:
    watches = book().load()["watches"]
    if not watches:
        return ("Aftercare isn't watching anything yet. It starts when a reply says something is deployed or "
                "live at a public address, or with /aftercare watch <url>.")
    every = _num("every_hours", 6)
    return "\n".join([f"Aftercare is watching {len(watches)} address{'es' if len(watches) != 1 else ''} "
                      f"(checked every {every:g}h, at session start):"] + [_status_line(w) for w in watches])


def check_now(check=None) -> str:
    b = book()
    watches = b.load()["watches"]
    if not watches:
        return listing()
    results = W.run_checks(b, watches, _num("timeout", 8), check or W.probe)
    up = sum(1 for _, r, _ in results if r.ok)
    out = [f"Checked {len(results)}: {up} answering, {len(results) - up} not."]
    for w, r, alert in results:
        note = ""
        if alert and alert["kind"] == "down":
            note = "  <- stopped working"
        elif alert and alert["kind"] == "never_up":
            note = "  <- has not worked yet"
        elif alert and alert["kind"] == "back":
            note = "  <- back up"
        out.append(f"  {'✅' if r.ok else '❌'} {w['url']}  {r.detail}, {r.ms} ms{note}")
    b.untold()                       # the user has just seen them
    return "\n".join(out)


def aftercare_command(raw_args: str = "", session_id: str = "", **_: Any) -> str:
    words = (raw_args or "").split()
    action = words[0].lower() if words else "list"
    try:
        if action in ("list", "status"):
            return listing()
        if action == "check":
            return check_now()
        if action == "watch" and len(words) == 2:
            url = W._strip(words[1])
            if not W.public(url):
                return f"Aftercare only watches public http(s) addresses, not {url}."
            added = book().add(url, "(added by you)", session_id or "", limit=int(_num("max_watches", 25)))
            return f"Watching {url}." if added else f"{url} is already watched."
        if action == "forget" and len(words) == 2:
            return "Stopped watching it." if book().forget(words[1]) else f"Nothing watched matches {words[1]}."
        return "Usage: /aftercare [list | check | watch <url> | forget <n|url>]"
    except Exception as exc:
        return f"Aftercare failed: {exc}"


def _cli_setup(parser: Any) -> None:
    parser.add_argument("action", nargs="?", default="list", choices=["list", "check"])


def _cli_run(args: Any) -> int:
    print(check_now() if getattr(args, "action", "list") == "check" else listing())
    return 0


def register(ctx: "PluginContext") -> None:
    global _CTX
    _CTX = ctx
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_command("aftercare", aftercare_command,
                         description="Re-check what the agent said was live, and hear when it breaks",
                         args_hint="[list|check|watch <url>|forget <n>]")
    try:
        ctx.register_cli_command("aftercare", "Re-check what your agent said was live",
                                 _cli_setup, _cli_run)
    except Exception:
        pass
