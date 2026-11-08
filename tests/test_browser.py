"""Unit tests for the parts of browser.py that run without a real browser."""

from __future__ import annotations

import asyncio
import json
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


# --- URLs ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "page", "param", "expected"),
    [
        ("https://example.com/search?q=x", 2, "page", "https://example.com/search?q=x&page=2"),
        ("https://example.com/search", 3, "p", "https://example.com/search?p=3"),
        ("https://example.com/s?page=1&q=a", 2, "page", "https://example.com/s?q=a&page=2"),
        ("https://example.com/s#top", 2, "page", "https://example.com/s?page=2#top"),
        ("https://example.com/s?flag=&q=a+b", 4, "page", "https://example.com/s?flag=&q=a+b&page=4"),
    ],
)
def test_build_paginated_url(url, page, param, expected):
    assert browser.build_paginated_url(url, page, param=param) == expected


def test_parse_cdp_endpoint():
    assert core._parse_cdp_endpoint("http://127.0.0.1:9333") == ("127.0.0.1", 9333)
    with pytest.raises(ValueError):
        core._parse_cdp_endpoint("http://127.0.0.1")
    with pytest.raises(ValueError):
        core._parse_cdp_endpoint("ws://127.0.0.1:9222")


# --- ports and profiles --------------------------------------------------


def test_each_browser_has_its_own_default_port():
    ports = [core.settings.default_port(name) for name in core.SUPPORTED_BROWSERS]
    assert ports == [9222, 9223, 9224]
    with pytest.raises(ValueError):
        core.settings.default_port("firefox")


def test_default_port_uses_main_profile():
    assert core._profile_dir("chrome", 9223).name == "chrome_profile"
    assert core._profile_dir("chrome", 9222).name == "chrome_profile_9222"


def test_remembered_port_round_trip_and_fallbacks():
    assert core._remembered_port("edge") == 9224
    core._remember_port("edge", 50123)
    assert core._remembered_port("edge") == 50123
    core._port_file("edge").write_text(json.dumps({"port": 70000}), encoding="utf-8")
    assert core._remembered_port("edge") == 9224
    core._port_file("edge").write_text("not json", encoding="utf-8")
    assert core._remembered_port("edge") == 9224


# --- start attempts ------------------------------------------------------


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


def test_abandon_attempt_retries_then_raises():
    server = core.CdpServer(9222, FakeProcess())
    core._abandon_attempt(server, OSError("boom"), last_attempt=False)
    assert server.process.terminated
    with pytest.raises(RuntimeError, match="Could not attach"):
        core._abandon_attempt(server, OSError("boom"), last_attempt=True)
    with pytest.raises(KeyboardInterrupt):
        core._abandon_attempt(server, KeyboardInterrupt(), last_attempt=False)


def test_stop_process_waits_for_clean_exit():
    clean = FakeProcess(exits=True)
    core._stop_process(clean, timeout=0)
    assert not clean.terminated


def test_stop_process_kills_a_hung_browser():
    hung = FakeProcess(exits=False)
    core._stop_process(hung, timeout=0)
    assert hung.terminated


def test_sync_close_asks_browser_to_close_before_detaching():
    calls = []
    cdp = SimpleNamespace(send=lambda method: calls.append(method))
    session = core.BrowserSession(
        playwright=SimpleNamespace(stop=lambda: calls.append("stop")),
        browser=SimpleNamespace(new_browser_cdp_session=lambda: cdp),
        context=None,
        page=None,
        cdp_url="http://127.0.0.1:9222",
        process=FakeProcess(),
    )
    session.close(terminate_process=True)
    assert calls == ["Browser.close", "stop"]
    assert not session.process.terminated


def test_sync_close_leaves_attached_browser_running():
    calls = []
    session = core.BrowserSession(
        playwright=SimpleNamespace(stop=lambda: calls.append("stop")),
        browser=SimpleNamespace(new_browser_cdp_session=lambda: pytest.fail("must not close")),
        context=None,
        page=None,
        cdp_url="http://127.0.0.1:9222",
        process=None,
    )
    session.close(terminate_process=True)
    assert calls == ["stop"]


# --- navigation and scraping ---------------------------------------------


class FakePage:
    def __init__(self, status: int = 200):
        self.status = status
        self.url = "https://example.com"
        self.goto_timeout = "unset"
        self.closed = False

    async def goto(self, url, *, wait_until, timeout):
        self.url = url
        self.goto_timeout = timeout
        return SimpleNamespace(status=self.status)

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


@pytest.mark.parametrize(("timeout", "expected_ms"), [(None, None), (0, 0), (2.5, 2500)])
def test_open_page_timeout(no_captcha, timeout, expected_ms):
    page = FakePage()
    asyncio.run(browser.open_page(page, "https://example.com", timeout=timeout))
    assert page.goto_timeout == expected_ms


def test_open_page_rejects_http_errors(no_captcha):
    with pytest.raises(RuntimeError, match="Shop returned HTTP 404"):
        asyncio.run(browser.open_page(FakePage(status=404), "https://x", site="Shop"))


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
    with pytest.raises(RuntimeError, match="scraping failed: bad b"):
        _scrape(context, ["a", "b", "c"], "raise")
    assert all(page.closed for page in context.pages)
