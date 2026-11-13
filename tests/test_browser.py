"""Unit tests for the parts of browser.py that run without a real browser."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

import browser
from browser import browser as core


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(core.settings, "data_dir", tmp_path)
    monkeypatch.setattr(core.settings, "cdp_port", 9222)


# --- package -------------------------------------------------------------


def test_package_exposes_public_api():
    for name in core.__all__:
        assert getattr(browser, name) is getattr(core, name)
    assert browser.settings is core.settings


# --- endpoints ----------------------------------------------------------


def test_parse_cdp_endpoint():
    assert core._parse_cdp_endpoint("http://127.0.0.1:9333") == ("127.0.0.1", 9333)
    with pytest.raises(ValueError):
        core._parse_cdp_endpoint("http://127.0.0.1")
    with pytest.raises(ValueError):
        core._parse_cdp_endpoint("ws://127.0.0.1:9222")


# --- ports and profiles --------------------------------------------------


def test_each_browser_has_its_own_port_and_profile(tmp_path):
    assert [core.settings.port(name) for name in core.SUPPORTED_BROWSERS] == [9222, 9223, 9224]
    assert core.settings.cdp_url("chrome") == "http://127.0.0.1:9223"
    assert core.settings.profile_dir("chrome") == tmp_path / "profiles" / "chrome_profile"
    with pytest.raises(ValueError):
        core.settings.port("firefox")


def test_ensure_browser_reuses_a_running_instance(monkeypatch):
    monkeypatch.setattr(core, "_cdp_is_up", lambda url: True)
    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    assert core._ensure_browser("brave", headless=True) is None


def test_ensure_browser_launches_on_the_persistent_profile(monkeypatch, tmp_path):
    launched = []
    process = FakeProcess()
    monkeypatch.setattr(core, "_cdp_is_up", lambda url: bool(launched))
    monkeypatch.setattr(core, "find_browser", lambda name: "brave-bin")
    monkeypatch.setattr(core.subprocess, "Popen", lambda args, **k: launched.append(args) or process)
    assert core._ensure_browser("brave", headless=False) is process
    assert f"--user-data-dir={tmp_path / 'profiles' / 'brave_profile'}" in launched[0]
    assert "--remote-debugging-port=9222" in launched[0]


def test_ensure_browser_stops_a_browser_that_never_serves_cdp(monkeypatch):
    process = FakeProcess()
    process.poll = lambda: 1
    monkeypatch.setattr(core, "_cdp_is_up", lambda url: False)
    monkeypatch.setattr(core, "find_browser", lambda name: "brave-bin")
    monkeypatch.setattr(core.subprocess, "Popen", lambda args, **k: process)
    with pytest.raises(RuntimeError, match="exited before exposing CDP"):
        core._ensure_browser("brave", headless=True)
    assert process.terminated


# --- starting and closing ----------------------------------------------


class FakeProcess:
    def __init__(self, exits: bool = True):
        self.exits = exits
        self.terminated = False

    def wait(self, timeout=None):
        if not self.exits and not self.terminated:
            raise subprocess.TimeoutExpired("browser", timeout)
        return 0

    def terminate(self):
        self.terminated = True


def test_stop_process_waits_for_clean_exit():
    clean = FakeProcess(exits=True)
    core._stop_process(clean, timeout=0)
    assert not clean.terminated


def test_stop_process_kills_a_hung_browser():
    hung = FakeProcess(exits=False)
    core._stop_process(hung, timeout=0)
    assert hung.terminated


def _session(calls, process):
    async def send(method):
        calls.append(method)

    async def new_browser_cdp_session():
        return SimpleNamespace(send=send)

    async def stop():
        calls.append("stop")

    return core.BrowserSession(
        playwright=SimpleNamespace(stop=stop),
        browser=SimpleNamespace(new_browser_cdp_session=new_browser_cdp_session),
        context=None,
        page=None,
        cdp_url="http://127.0.0.1:9222",
        process=process,
    )


def test_close_asks_launched_browser_to_close_before_detaching():
    calls = []
    session = _session(calls, FakeProcess())
    asyncio.run(session.close(stop_browser=True))
    assert calls == ["Browser.close", "stop"]
    assert not session.process.terminated


def test_close_leaves_attached_browser_running():
    calls = []
    asyncio.run(_session(calls, process=None).close(stop_browser=True))
    assert calls == ["stop"]


def test_solve_captcha_needs_an_endpoint_for_foreign_pages():
    class UnknownBrowser:
        pass

    page = SimpleNamespace(context=SimpleNamespace(browser=UnknownBrowser()))
    with pytest.raises(ValueError, match="cdp_url"):
        asyncio.run(browser.solve_captcha(page))


# --- navigation and scraping ---------------------------------------------


class FakePage:
    def __init__(self, status: int = 200):
        self.status = status
        self.url = "https://example.com"
        self.closed = False

    async def goto(self, url, *, wait_until):
        self.url = url
        return SimpleNamespace(status=self.status, ok=self.status < 400)

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, failing: set[str]):
        self.failing = failing
        self.pages: list[FakePage] = []

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


@pytest.fixture
def no_captcha(monkeypatch):
    async def has_captcha(page):
        return False

    monkeypatch.setattr(core, "has_captcha", has_captcha)


def test_load_rejects_http_errors(no_captcha):
    with pytest.raises(RuntimeError, match="HTTP 404 at https://x"):
        asyncio.run(core._load(FakePage(status=404), "https://x"))


def test_load_skips_status_check_once_captcha_is_solved(monkeypatch):
    async def has_captcha(page):
        return True

    async def solve_captcha(page):
        return True

    monkeypatch.setattr(core, "has_captcha", has_captcha)
    monkeypatch.setattr(core, "solve_captcha", solve_captcha)
    asyncio.run(core._load(FakePage(status=403), "https://x"))


def _scrape(context, urls, on_error):
    async def extractor(tab, url):
        if url in context.failing:
            raise ValueError(f"bad {url}")
        return url.upper()

    return asyncio.run(browser.scrape_pages(context, urls, extractor, max_concurrent_tabs=2, on_error=on_error))


def test_scrape_pages_skip_keeps_results_aligned(no_captcha):
    context = FakeContext(failing={"b"})
    assert _scrape(context, ["a", "b", "c"], "skip") == ["A", None, "C"]
    assert all(page.closed for page in context.pages)


def test_scrape_pages_raise_reports_failure(no_captcha):
    context = FakeContext(failing={"b"})
    with pytest.raises(ValueError, match="bad b"):
        _scrape(context, ["a", "b", "c"], "raise")
    assert all(page.closed for page in context.pages)
