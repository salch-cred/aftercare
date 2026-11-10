"""Aftercare as Hermes drives it, with scripted network checks."""

import argparse
import time

import pytest
import aftercare
from aftercare import watch as W


class FakeCtx:
    def __init__(self, settings=None):
        self.settings, self.hooks, self.commands, self.cli = settings or {}, {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, cb, **_):
        self.hooks.setdefault(name, []).append(cb)

    def register_command(self, name, handler, **_):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **_):
        self.cli[name] = (setup_fn, handler_fn)

    def fire(self, name, **kw):
        return [cb(**kw) for cb in self.hooks.get(name, [])]


@pytest.fixture
def ctx():
    fake = FakeCtx()
    aftercare.register(fake)
    return fake


class Net:
    """Scripted answers per URL."""

    def __init__(self, **status):
        self.status = {f"https://{k.replace('_', '.')}": v for k, v in status.items()}
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        code = self.status.get(url, 200)
        if code is None:
            return W.Result(False, None, "no answer within 8s", 8000)
        return W.Result(code < 400, code, f"HTTP {code}", 42)


def reply(ctx, text, session="S1"):
    ctx.fire("post_llm_call", session_id=session, assistant_response=text)


# finding claims -----------------------------------------------------------

@pytest.mark.parametrize("text,urls", [
    ("Deployed! The shop is live at https://shop.example.com.", ["https://shop.example.com"]),
    ("I pushed it and the docs are now available at https://docs.example.org/v2 for everyone.", ["https://docs.example.org/v2"]),
    ("The API is up and running on https://api.example.com/health", ["https://api.example.com/health"]),
    ("Published to https://a.example.com and https://b.example.com.", ["https://a.example.com", "https://b.example.com"]),
    ("Done. You can now see it at https://x.example.dev", ["https://x.example.dev"]),
])
def test_finds_live_claims(text, urls):
    assert [u for u, _ in W.claims(text)] == urls


@pytest.mark.parametrize("text", [
    "Read the guide at https://docs.python.org/3/library/urllib.html for details.",   # a link, not a claim
    "The deploy to https://shop.example.com failed with a 502.",
    "It is not live at https://shop.example.com yet.",
    "Once DNS updates it will be live at https://shop.example.com.",
    "Is it live at https://shop.example.com?",
    "The dev server is running at http://localhost:3000.",
    "Running at http://192.168.1.20:8080 on your network.",
    "```\ndeployed to https://shop.example.com\n```",
])
def test_ignores_non_claims_and_private_addresses(text):
    assert W.claims(text) == []


@pytest.mark.parametrize("url,ok", [
    ("https://shop.example.com", True), ("http://example.org/x", True),
    ("http://localhost:3000", False), ("http://127.0.0.1", False), ("http://10.0.0.5", False),
    ("http://[::1]:80", False), ("http://printer.local", False), ("ftp://example.com", False),
    ("https://intranet", False),
])
def test_public(url, ok):
    assert W.public(url) is ok


# the flow ---------------------------------------------------------------

def test_claim_is_watched_and_listed(ctx):
    reply(ctx, "Deployed. The shop is live at https://shop.example.com.")
    out = ctx.commands["aftercare"]("")
    assert "watching 1 address" in out and "https://shop.example.com" in out and "not checked yet" in out
    assert "The shop is live at https://shop.example.com." in out


def test_breaks_later_and_the_agent_is_told_once(ctx):
    reply(ctx, "Deployed. The shop is live at https://shop.example.com.")
    aftercare.check_due(wait=True, check=Net())                 # first check: up, nothing to say
    assert ctx.fire("pre_llm_call", session_id="S2") == [None]
    b = aftercare.book()
    b.update(lambda d: [w.__setitem__("checked", 0) for w in d["watches"]])      # due again
    aftercare.check_due(wait=True, check=Net(shop_example_com=500))
    told = ctx.fire("pre_llm_call", session_id="S3")[0]["context"]
    assert "https://shop.example.com stopped working after you said on" in told
    assert "Now: HTTP 500" in told and "Tell the user" in told
    assert ctx.fire("pre_llm_call", session_id="S3") == [None]                  # only once


def test_never_up_and_back(ctx):
    reply(ctx, "The API is up and running on https://api.example.com")
    aftercare.check_due(wait=True, check=Net(api_example_com=None))
    assert "has not worked since" in ctx.fire("pre_llm_call")[0]["context"]
    aftercare.book().update(lambda d: [w.__setitem__("checked", 0) for w in d["watches"]])
    aftercare.check_due(wait=True, check=Net())
    assert "is answering again (HTTP 200)" in ctx.fire("pre_llm_call")[0]["context"]


def test_still_down_is_not_repeated(ctx):
    reply(ctx, "Deployed to https://shop.example.com, it is live.")
    for _ in range(3):
        aftercare.book().update(lambda d: [w.__setitem__("checked", 0) for w in d["watches"]])
        aftercare.check_due(wait=True, check=Net(shop_example_com=503))
    assert len(aftercare.book().load()["alerts"]) == 1


def test_not_due_is_not_checked(ctx):
    reply(ctx, "Deployed. The shop is live at https://shop.example.com.")
    net = Net()
    aftercare.check_due(wait=True, check=net)
    aftercare.check_due(wait=True, check=net)
    assert net.calls == ["https://shop.example.com"]


def test_check_now_reports_and_marks_seen(ctx, monkeypatch):
    reply(ctx, "Live at https://a.example.com and deployed https://b.example.com too.")
    monkeypatch.setattr(W, "probe", Net(b_example_com=500))
    out = ctx.commands["aftercare"]("check")
    assert "Checked 2: 1 answering, 1 not." in out and "❌ https://b.example.com  HTTP 500" in out
    assert "has not worked yet" in out
    assert ctx.fire("pre_llm_call") == [None]                  # already seen in the command


def test_session_start_runs_in_the_background(ctx, monkeypatch):
    reply(ctx, "Deployed. The shop is live at https://shop.example.com.")
    seen = []
    monkeypatch.setattr(W, "probe", lambda url, timeout: (seen.append(url), time.sleep(0.05), W.Result(True, 200, "HTTP 200", 1))[2])
    started = time.monotonic()
    ctx.fire("on_session_start", session_id="S2")
    assert time.monotonic() - started < 0.05                    # did not wait for the network
    for _ in range(100):
        if aftercare.book().load()["watches"][0]["checked"]:
            break
        time.sleep(0.01)
    assert seen == ["https://shop.example.com"]


def test_watch_forget_and_usage(ctx):
    cmd = ctx.commands["aftercare"]
    assert cmd("watch https://status.example.com") == "Watching https://status.example.com."
    assert "already watched" in cmd("watch https://status.example.com")
    assert "only watches public" in cmd("watch http://localhost:8000")
    assert "(added by you)" in cmd("list")
    assert cmd("forget 1") == "Stopped watching it."
    assert "Nothing watched matches" in cmd("forget 1")
    assert "Usage" in cmd("frobnicate")


def test_auto_watch_off():
    fake = FakeCtx({"auto_watch": False})
    aftercare.register(fake)
    reply(fake, "Deployed. The shop is live at https://shop.example.com.")
    assert "isn't watching anything" in fake.commands["aftercare"]("")


def test_old_claims_expire(ctx):
    reply(ctx, "Deployed. The shop is live at https://shop.example.com.")
    aftercare.book().update(lambda d: d["watches"][0].__setitem__("claimed", time.time() - 40 * 86400))
    aftercare.check_due(wait=True, check=Net())
    assert aftercare.book().load()["watches"] == []


def test_watch_limit_drops_the_oldest():
    fake = FakeCtx({"max_watches": 2})
    aftercare.register(fake)
    for name in ("a", "b", "c"):
        reply(fake, f"Deployed, live at https://{name}.example.com.")
    assert [w["url"] for w in aftercare.book().load()["watches"]] == ["https://b.example.com", "https://c.example.com"]


def test_probe_refuses_private_addresses():
    assert W.probe("http://127.0.0.1:9").detail == "not a public address"


def test_probe_refuses_names_that_resolve_privately(monkeypatch):
    monkeypatch.setattr(W.socket, "getaddrinfo", lambda host, port: [(None, None, None, None, ("10.1.2.3", 0))])
    assert "private address" in W.probe("https://sneaky.example.com").detail


def test_cli(ctx, capsys):
    reply(ctx, "Deployed. The shop is live at https://shop.example.com.")
    setup, run = ctx.cli["aftercare"]
    parser = argparse.ArgumentParser()
    setup(parser)
    assert run(parser.parse_args([])) == 0
    assert "https://shop.example.com" in capsys.readouterr().out


def test_hooks_never_raise_on_junk(ctx):
    assert ctx.fire("post_llm_call", assistant_response=None) == [None]
    assert ctx.fire("on_session_start") == [None]
