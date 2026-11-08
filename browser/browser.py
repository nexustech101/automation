"""
Stealth browser automation and scraping over CDP, in one file.

The browser (Brave, Chrome or Edge) runs as an ordinary process with its own
automation profile, so logins persist between runs. Patchright attaches to it
over the Chrome DevTools Protocol, which means sites see a normal browser
instead of a driver-launched one. session.page is an ordinary Patchright
page. SeleniumBase solves CAPTCHAs on the same tabs, and scrape_pages() reads
many pages in parallel tabs.

Requires Python 3.12+ and:
    pip install patchright        # required
    pip install seleniumbase      # only for solve_captcha()

Public API, in file order (see README.md for a guide):
    Settings      settings, Settings, find_browser
    Sessions      async_browser_session, BrowserSession
    Scraping      scrape_pages, build_paginated_url
    CAPTCHA       has_captcha, solve_captcha, wait_for_captcha_clear  (SeleniumBase)
    LLM outline   minimize_dom

*Usage:*
>>> import asyncio
>>> import browser
>>> async def main():
...     async with browser.async_browser_session("brave") as session:
...         await session.page.goto("https://example.com")
...         print(await session.page.title())
>>> asyncio.run(main())
Example Domain
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import weakref
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable, Literal, TypeVar

from patchright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

__all__ = [
    "BrowserSession",
    "Settings",
    "async_browser_session",
    "build_paginated_url",
    "find_browser",
    "has_captcha",
    "minimize_dom",
    "scrape_pages",
    "settings",
    "solve_captcha",
    "wait_for_captcha_clear",
]

logger = logging.getLogger(__name__)

BrowserName = Literal["brave", "chrome", "edge"]
SUPPORTED_BROWSERS: tuple[BrowserName, ...] = ("brave", "chrome", "edge")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class Settings:
    """
    Defaults read each time a browser is started. Change them once, before
    the first session. The default data_dir is shared with every project
    that uses this file, so they all reuse the same signed-in profiles.
    cdp_port is Brave's default port; Chrome uses the next port and Edge the
    one after, so one browser is never mistaken for another.

    *Usage:*
    >>> browser.settings.data_dir = Path("~/.my_scraper").expanduser()
    >>> browser.settings.executables["brave"] = "D:/Apps/Brave/brave.exe"
    >>> browser.settings.cdp_port = 9333  # Brave 9333, Chrome 9334, Edge 9335
    """

    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    data_dir: Path = field(default_factory=lambda: Path.home() / ".browser_automation")
    executables: dict[str, str] = field(default_factory=dict)
    attach_timeout: float = 5.0
    launch_timeout: float = 15.0

    @property
    def profile_root(self) -> Path:
        return self.data_dir / "profiles"

    def default_port(self, browser: BrowserName) -> int:
        """The browser's own default CDP port, which uses its main profile."""
        _require_supported(browser)
        return self.cdp_port + SUPPORTED_BROWSERS.index(browser)


settings = Settings()

# Seconds a closing browser gets to save its profile before it is killed.
_SHUTDOWN_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


@dataclass
class BrowserSession:
    """
    A Patchright handle on a running browser, from async_browser_session().
    `page` is the browser's first tab and `context` its default context; use
    them with the normal Patchright API. `process` is set only when this
    session launched the browser.
    """

    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    cdp_url: str
    process: subprocess.Popen | None = None

    async def close(self, stop_browser: bool = False) -> None:
        """
        Detach. With stop_browser, a browser this session launched is asked
        to shut down cleanly first, so its profile and logins are saved.
        """
        stop_browser = stop_browser and self.process is not None
        try:
            if stop_browser:
                with suppress(Exception):  # The connection drops as the browser exits.
                    cdp = await self.browser.new_browser_cdp_session()
                    await cdp.send("Browser.close")
            await self.playwright.stop()
        finally:
            if stop_browser:
                await asyncio.to_thread(_stop_process, self.process)


@asynccontextmanager
async def async_browser_session(
    browser: BrowserName = "brave",
    *,
    cdp_url: str | None = None,
    headless: bool = False,
    keep_open: bool = False,
) -> AsyncGenerator[BrowserSession]:
    """
    Start or attach a browser for the duration of the block. Without cdp_url
    it reuses the browser's running automation instance or launches one; if
    attaching fails, it retries once on a fresh port and profile, which may
    need signing in again. With cdp_url it only attaches and never launches.
    On exit it detaches, and closes a browser it launched unless keep_open.

    *Usage:*
    >>> async with async_browser_session("brave") as session:
    ...     await session.page.goto("https://example.com")
    >>> async with async_browser_session(cdp_url="http://127.0.0.1:9222") as session:
    ...     print(session.page.url)
    """
    if cdp_url is None:
        session = await _start_session(browser, headless)
    else:
        await asyncio.to_thread(_wait_for_cdp, cdp_url, settings.launch_timeout)
        session = await _attach(cdp_url)
    try:
        yield session
    finally:
        # Room for a clean shutdown, then the forced one.
        async with asyncio.timeout(_SHUTDOWN_TIMEOUT * 3):
            await session.close(stop_browser=not keep_open)


async def _start_session(browser: BrowserName, headless: bool) -> BrowserSession:
    """Reuse or launch on the usual port; if that fails, retry on a fresh port and profile."""
    server = await asyncio.to_thread(_start_cdp, browser, headless, fresh=False)
    try:
        session = await _attach_server(server)
    except Exception as exc:
        logger.warning("Retrying %s on a fresh profile: %s", browser, exc)
        server = await asyncio.to_thread(_start_cdp, browser, headless, fresh=True)
        session = await _attach_server(server)
    # Saved only after attaching, so a broken port is never reused.
    _remember_port(browser, server.port)
    return session


async def _attach_server(server: _CdpServer) -> BrowserSession:
    """Attach to the server, stopping the browser it launched if that fails."""
    try:
        session = await _attach(server.url)
    except BaseException:
        server.terminate()
        raise
    session.process = server.process
    return session


async def _attach(endpoint: str) -> BrowserSession:
    try:
        session = await _connect(endpoint)
    except Exception as exc:
        raise RuntimeError(f"Could not attach to browser CDP at {endpoint}: {exc}") from exc
    _ENDPOINTS[session.browser] = endpoint
    return session


async def _connect(endpoint: str) -> BrowserSession:
    playwright = None
    try:
        # Bounds driver startup and context/page setup as well as the connection.
        async with asyncio.timeout(settings.attach_timeout * 2):
            playwright = await async_playwright().start()
            browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=settings.attach_timeout * 1000)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
    except BaseException:
        if playwright is not None:
            with suppress(Exception):
                async with asyncio.timeout(5):
                    await playwright.stop()
        raise
    return BrowserSession(playwright, browser, context, page, cdp_url=endpoint)


def _stop_process(process: subprocess.Popen, timeout: float = _SHUTDOWN_TIMEOUT) -> None:
    """Wait for a browser that was asked to close; kill it only if it hangs."""
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout)


# The endpoint each session attached through, so solve_captcha(page) can
# point SeleniumBase at the same browser without the caller passing it.
_ENDPOINTS: weakref.WeakKeyDictionary[Browser, str] = weakref.WeakKeyDictionary()


# --------------------------------------------------------------------------
# Browser processes and CDP endpoints
# --------------------------------------------------------------------------


def find_browser(browser: BrowserName) -> str:
    """
    The browser's executable: settings.executables if set, otherwise the
    standard install locations for this OS, otherwise the PATH.

    *Usage:*
    >>> find_browser("brave")  # e.g. C:/Program Files/BraveSoftware/.../brave.exe
    >>> browser.settings.executables["brave"] = "D:/Apps/Brave/brave.exe"  # override
    """
    _require_supported(browser)
    if configured := settings.executables.get(browser):
        if Path(configured).exists():
            return configured
        raise FileNotFoundError(f"settings.executables[{browser!r}] does not exist: {configured}")

    candidates = [Path(os.path.expandvars(path)).expanduser() for path in _INSTALL_PATHS[browser].get(_SYSTEM, [])]
    installed = next((str(path) for path in candidates if path.exists()), None)
    installed = installed or next(filter(None, map(shutil.which, _PATH_COMMANDS[browser])), None)
    if installed is None:
        raise FileNotFoundError(
            f"Could not find {browser!r} on {_SYSTEM}. Install it or set "
            f"browser.settings.executables[{browser!r}]. Checked: {[str(path) for path in candidates]}"
        )
    return installed


@dataclass
class _CdpServer:
    """A CDP endpoint, plus its process when this module launched it."""

    port: int
    process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://{settings.cdp_host}:{self.port}"

    def terminate(self) -> None:
        if self.process is not None:
            self.process.terminate()


def _start_cdp(browser: BrowserName, headless: bool, *, fresh: bool) -> _CdpServer:
    """
    Reuse the browser's last working endpoint or its default port if one is
    live, otherwise launch it. fresh=True always launches on a new port with
    its own profile, for recovering from a stuck instance.
    """
    with _START_LOCK:
        if fresh:
            port = _free_port()
        else:
            port = _remembered_port(browser)
            for live_port in dict.fromkeys((port, settings.default_port(browser))):
                if _cdp_is_up(_CdpServer(live_port).url):
                    return _CdpServer(live_port)
        return _CdpServer(port, _launch_browser(browser, headless, port))


def _launch_browser(browser: BrowserName, headless: bool, port: int) -> subprocess.Popen:
    """Launch the browser with the port's profile and wait for CDP, or stop it."""
    profile = _profile_dir(browser, port)
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            find_browser(browser),
            f"--remote-debugging-address={settings.cdp_host}",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--headless=new" if headless else "--start-maximized",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_cdp(_CdpServer(port).url, settings.launch_timeout, process)
    except BaseException:
        process.terminate()
        raise
    return process


def _cdp_is_up(cdp_url: str) -> bool:
    try:
        with _DIRECT_OPENER.open(f"{cdp_url.rstrip('/')}/json/version", timeout=0.5) as response:
            data = json.load(response)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("webSocketDebuggerUrl"))


def _wait_for_cdp(cdp_url: str, timeout: float, process: subprocess.Popen | None = None) -> None:
    """Block until the endpoint answers. Fails early if `process` exits first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cdp_is_up(cdp_url):
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError("The browser exited before exposing CDP. Its automation profile may already be in use.")
        time.sleep(0.2)
    raise TimeoutError(f"Browser CDP endpoint never came up at {cdp_url}")


def _require_supported(browser: str) -> None:
    if browser not in SUPPORTED_BROWSERS:
        raise ValueError(f"Unsupported browser {browser!r}; use one of {', '.join(SUPPORTED_BROWSERS)}.")


def _profile_dir(browser: BrowserName, port: int) -> Path:
    suffix = "" if port == settings.default_port(browser) else f"_{port}"
    return settings.profile_root / f"{browser}_profile{suffix}"


def _remembered_port(browser: BrowserName) -> int:
    """The port of the browser's last successful session, else its default."""
    default = settings.default_port(browser)
    try:
        port = json.loads(_port_file(browser).read_text(encoding="utf-8"))["port"]
    except (OSError, ValueError, KeyError, TypeError):
        return default
    return port if type(port) is int and 0 < port < 65536 else default


def _remember_port(browser: BrowserName, port: int) -> None:
    settings.profile_root.mkdir(parents=True, exist_ok=True)
    _port_file(browser).write_text(json.dumps({"port": port}), encoding="utf-8")


def _port_file(browser: BrowserName) -> Path:
    return settings.profile_root / f"{browser}_cdp.json"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind((settings.cdp_host, 0))
        return listener.getsockname()[1]


_START_LOCK = threading.Lock()

# CDP lives on loopback; never route its probes through HTTP(S)_PROXY or the
# Windows system proxy, which can answer for 127.0.0.1 and fake an endpoint.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_SYSTEM = platform.system()  # "Windows", "Darwin" or "Linux"

_INSTALL_PATHS: dict[str, dict[str, list[str]]] = {
    "brave": {
        "Windows": [
            "%PROGRAMFILES%/BraveSoftware/Brave-Browser/Application/brave.exe",
            "%PROGRAMFILES(X86)%/BraveSoftware/Brave-Browser/Application/brave.exe",
            "%LOCALAPPDATA%/BraveSoftware/Brave-Browser/Application/brave.exe",
        ],
        "Darwin": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
        "Linux": ["/usr/bin/brave-browser", "/opt/brave.com/brave/brave"],
    },
    "chrome": {
        "Windows": [
            "%PROGRAMFILES%/Google/Chrome/Application/chrome.exe",
            "%PROGRAMFILES(X86)%/Google/Chrome/Application/chrome.exe",
            "%LOCALAPPDATA%/Google/Chrome/Application/chrome.exe",
        ],
        "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "Linux": ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"],
    },
    "edge": {
        "Windows": [
            "%PROGRAMFILES(X86)%/Microsoft/Edge/Application/msedge.exe",
            "%PROGRAMFILES%/Microsoft/Edge/Application/msedge.exe",
        ],
        "Darwin": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "Linux": ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"],
    },
}

_PATH_COMMANDS: dict[str, tuple[str, ...]] = {
    "brave": ("brave-browser", "brave"),
    "chrome": ("google-chrome", "google-chrome-stable", "chrome"),
    "edge": ("microsoft-edge", "microsoft-edge-stable", "msedge"),
}


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------


Extracted = TypeVar("Extracted")


async def scrape_pages(
    context: BrowserContext,
    urls: list[str],
    extractor: Callable[[Page, str], Awaitable[Extracted]],
    *,
    max_concurrent_tabs: int = 5,
    on_error: Literal["raise", "skip"] = "raise",
) -> list[Extracted | None]:
    """
    Open each url in its own tab and return `extractor(tab, url)` results in
    url order. Before the extractor runs, each tab solves any CAPTCHA it
    lands on and otherwise rejects HTTP errors. Tabs are always closed.
    on_error="raise" cancels the remaining tabs and re-raises the first
    failure; on_error="skip" logs each failed url and puts None in its place.

    *Usage:*
    >>> async def title(tab, url):
    ...     return await tab.title()
    >>> await scrape_pages(session.context, urls, title, max_concurrent_tabs=3)
    ['Page one', 'Page two']
    >>> await scrape_pages(session.context, urls, title, on_error="skip")
    ['Page one', None]
    """
    if max_concurrent_tabs < 1:
        raise ValueError("max_concurrent_tabs must be at least 1.")
    semaphore = asyncio.Semaphore(max_concurrent_tabs)

    async def scrape(url: str) -> Extracted:
        async with semaphore:
            tab = await context.new_page()
            try:
                await _load(tab, url)
                return await extractor(tab, url)
            finally:
                await tab.close()

    if on_error == "raise":
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(scrape(url)) for url in urls]
        except ExceptionGroup as exc:
            raise exc.exceptions[0]
        return [task.result() for task in tasks]

    outcomes = await asyncio.gather(*map(scrape, urls), return_exceptions=True)
    for url, outcome in zip(urls, outcomes):
        if isinstance(outcome, Exception):
            logger.warning("Skipping %s: %s", url, outcome)
    return [None if isinstance(outcome, Exception) else outcome for outcome in outcomes]


async def _load(tab: Page, url: str) -> None:
    """
    Navigate, then solve any CAPTCHA or reject an HTTP error. Anti-bot walls
    answer 403/429 before the real page loads, so a solved challenge skips
    the status check.
    """
    response = await tab.goto(url, wait_until="domcontentloaded")
    if await has_captcha(tab):
        if not await solve_captcha(tab):
            raise RuntimeError(f"CAPTCHA at {url} was not solved; solve it in the browser and rerun.")
    elif response is not None and not response.ok:
        raise RuntimeError(f"HTTP {response.status} at {url}")


def build_paginated_url(base_url: str, page_num: int, *, param: str = "page") -> str:
    """
    Set the page parameter in a URL's query, replacing any existing value
    and keeping the other parameters and the #fragment.

    *Usage:*
    >>> build_paginated_url("https://example.com/search?q=x", 2)
    'https://example.com/search?q=x&page=2'
    >>> build_paginated_url("https://example.com/search?page=1#top", 3)
    'https://example.com/search?page=3#top'
    """
    parts = urllib.parse.urlsplit(base_url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key != param
    ]
    query.append((param, str(page_num)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# --------------------------------------------------------------------------
# CAPTCHA (SeleniumBase sb.solve_captcha())
# --------------------------------------------------------------------------


async def has_captcha(page: Page) -> bool:
    """
    True while the page shows an unsolved Cloudflare, reCAPTCHA, hCaptcha or
    DataDome challenge, judged by SeleniumBase's own page markers.

    *Usage:*
    >>> if await has_captcha(session.page):
    ...     await solve_captcha(session.page)
    """
    for _ in range(_CAPTCHA_READ_ATTEMPTS):
        try:
            return await page.evaluate(_CAPTCHA_JS)
        except PlaywrightError:  # Challenge pages reload themselves mid-read; read the new document.
            await page.wait_for_load_state("domcontentloaded")
    return False


async def solve_captcha(page: Page, *, cdp_url: str | None = None, timeout: float = 15.0) -> bool:
    """
    Run SeleniumBase's sb.solve_captcha() on this exact tab, then wait up to
    `timeout` seconds for the challenge to clear. SeleniumBase attaches to the
    browser's CDP endpoint and detaches without closing anything; cdp_url
    defaults to the endpoint async_browser_session() attached through.
    Image puzzles that follow a checkbox still need a person.

    *Usage:*
    >>> async with async_browser_session() as session:
    ...     await session.page.goto("https://gitlab.com/users/sign_in")
    ...     await solve_captcha(session.page)
    True
    """
    endpoint = cdp_url or _ENDPOINTS.get(page.context.browser)
    if endpoint is None:
        raise ValueError("Pass cdp_url for a page that was not opened through async_browser_session().")
    host, port = _parse_cdp_endpoint(endpoint)
    target_id = await _target_id(page)
    await asyncio.to_thread(_seleniumbase_solve, host, port, target_id)
    return await wait_for_captcha_clear(page, timeout)


async def wait_for_captcha_clear(page: Page, timeout: float) -> bool:
    """
    Poll has_captcha() until the page is clear; False after `timeout` seconds.
    Useful when a person has to finish the challenge.

    *Usage:*
    >>> await wait_for_captcha_clear(session.page, timeout=300)
    True
    """
    deadline = time.monotonic() + timeout
    while await has_captcha(page):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_CAPTCHA_POLL_SECONDS)
    return True


def _seleniumbase_solve(host: str, port: int, target_id: str) -> None:
    """Attach SeleniumBase's CDP driver to one tab, call solve_captcha(), detach."""
    try:
        from seleniumbase.core.sb_cdp import CDPMethods
        from seleniumbase.undetected.cdp_driver import cdp_util
    except ImportError as exc:
        raise ImportError("solve_captcha() needs SeleniumBase: pip install seleniumbase") from exc

    with _SELENIUMBASE_LOCK, asyncio.Runner() as runner:
        loop = runner.get_loop()
        sb_browser = loop.run_until_complete(cdp_util.start_async(host=host, port=port))
        try:
            tab = next((tab for tab in sb_browser.tabs if str(tab.target.target_id) == target_id), None)
            if tab is None:
                raise RuntimeError("The page's tab was not found on the SeleniumBase endpoint.")
            # scrape_pages() runs tabs in parallel; the challenge must be the visible tab.
            loop.run_until_complete(tab.activate())
            CDPMethods(loop, tab, sb_browser).solve_captcha()
        finally:
            # Close transports only: quit()/stop() would close the caller's browser.
            for connection in [*sb_browser.tabs, sb_browser.connection]:
                loop.run_until_complete(connection.aclose())


def _parse_cdp_endpoint(cdp_url: str) -> tuple[str, int]:
    endpoint = urllib.parse.urlsplit(cdp_url)
    if endpoint.scheme != "http" or not endpoint.hostname or not endpoint.port:
        raise ValueError("cdp_url must be the browser's HTTP CDP endpoint, including its port.")
    return endpoint.hostname, endpoint.port


async def _target_id(page: Page) -> str:
    """The DevTools target ID SeleniumBase uses to find this tab."""
    cdp = await page.context.new_cdp_session(page)
    try:
        return (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
    finally:
        await cdp.detach()


# SeleniumBase keeps process-global state, so only one solve runs at a time.
_SELENIUMBASE_LOCK = threading.Lock()
_CAPTCHA_READ_ATTEMPTS = 3
_CAPTCHA_POLL_SECONDS = 0.25

# SeleniumBase's page markers (sb_cdp._on_a_cf_turnstile_page and
# _on_a_g_recaptcha_page), checked with selectors rather than by serializing
# the whole page, since this runs on every navigation and every poll.
# "/challenge-platform/h/b/" alone is ignored because Cloudflare injects that
# beacon on every page it proxies. A filled response token means the widget
# has already been solved.
_CAPTCHA_JS = r"""() => {
    const present = selector => !!document.querySelector(selector);

    const cloudflareInterstitial = /^just a moment/i.test(document.title) || '_cf_chl_opt' in window;
    const turnstile = present('[id^="challenge-widget-"], [id*="cf-turnstile-"], [class*="cf-turnstile-"], '
            + '[name*="cf-turnstile-"], [src*="challenges.cloudf"]')
        || (present('[data-callback="onCaptchaSuccess"]') && !present('[title="reCAPTCHA"]'));
    const recaptcha = present('iframe[title="reCAPTCHA"]:not(.grecaptcha-badge iframe)');
    const hcaptcha = present('iframe[data-hcaptcha-widget-id]');
    const datadome = present('iframe[src*="captcha-delivery.com/captcha/"]');

    const solved = [...document.querySelectorAll('input[name="cf-turnstile-response"], '
        + 'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"]')]
        .some(el => el.value.trim());
    return (cloudflareInterstitial || turnstile || recaptcha || hcaptcha || datadome) && !solved;
}"""


# --------------------------------------------------------------------------
# DOM outline for LLMs
# --------------------------------------------------------------------------


async def minimize_dom(
    page: Page,
    *,
    sample_repeated: bool = False,
    max_depth: int = 18,
    max_text: int = 150,
    max_attr: int = 200,
    max_classes: int = 6,
    max_lines: int = 1500,
    keep_hidden: bool = False,
    collapse_wrappers: bool = True,
) -> str:
    """
    A compact, indented outline of <body> for an LLM to read, built in one
    evaluate() call. It keeps ARIA and selector-friendly attributes and live
    form values, and drops scripts, styles and hidden elements. Password and
    file input values are never included, but it is not a general redaction
    tool. sample_repeated collapses long runs of identical siblings (form
    fields are never collapsed).

    *Usage:*
    >>> outline = await minimize_dom(session.page, sample_repeated=True)
    """
    return await page.locator("body").evaluate(_MINIMIZE_DOM_JS, {
        "sampleRepeated": sample_repeated,
        "maxDepth": max_depth,
        "maxText": max_text,
        "maxAttr": max_attr,
        "maxClasses": max_classes,
        "maxLines": max_lines,
        "keepHidden": keep_hidden,
        "collapseWrappers": collapse_wrappers,
    })


_MINIMIZE_DOM_JS = r"""
(root, o) => {
    // Compared against localName: SVG elements report lowercase tagNames.
    const IGNORED = new Set([
        "script", "style", "noscript", "template", "link", "meta",
        "svg", "canvas", "br", "hr", "wbr", "source", "track", "col", "colgroup",
    ]);

    // Attribute-less pass-through containers that are safe to flatten.
    const WRAPPERS = new Set(["div", "span"]);

    // Formatting tags whose text is merged into the parent's text line.
    const INLINE = new Set(["b", "i", "em", "strong", "u", "s", "small", "mark", "sub", "sup", "code"]);

    const KEEP = new Set([
        "id", "class", "href", "src", "title", "alt", "role", "name", "type",
        "value", "placeholder", "for", "itemprop",
        "disabled", "checked", "selected", "readonly", "required", "multiple",
        "contenteditable", "target", "action", "method",
    ]);
    const BOOL = new Set([
        "disabled", "checked", "selected", "readonly", "required", "multiple",
        "contenteditable",
    ]);

    // data-* is mostly framework noise; keep only the selector-friendly ones.
    const DATA_KEEP = /^data-(testid|test-id|test|qa|cy|automation-id|id|action|role|name|type|state)$/;

    // Generated class names (CSS-in-JS / CSS modules) carry no meaning.
    const GENERATED = /^(css|sc|jsx|svelte|emotion|styled)-[\w-]{4,}$|^_[\w-]{5,}$/;

    // State classes that should not break up a run of "identical" siblings.
    const STATE = /(^|[-_])(active|selected|current|open|disabled|checked|focused?|hover|first|last|odd|even)$/;

    const EDITABLE = "input:not([type=hidden]), select, textarea, [contenteditable=\"\"], [contenteditable=true]";

    const lines = [];
    let truncated = false;

    const squash = s => s.replace(/\s+/g, " ").trim();
    const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

    const hiddenCache = new WeakMap();
    function isHidden(el) {
        if (o.keepHidden) return false;
        let hidden = hiddenCache.get(el);
        if (hidden === undefined) {
            hidden = el.hidden === true ||
                (el.localName === "input" && el.type === "hidden") ||
                getComputedStyle(el).display === "none";
            hiddenCache.set(el, hidden);
        }
        return hidden;
    }

    const inlinable = c =>
        INLINE.has(c.localName) &&
        c.children.length === 0 &&
        [...c.attributes].every(a => a.name === "class" || a.name === "style");

    // Light DOM children plus open shadow-root children.
    const kidsOf = el =>
        [...(el.shadowRoot ? el.shadowRoot.children : []), ...el.children]
            .filter(c => !IGNORED.has(c.localName) && !inlinable(c) && !isHidden(c));

    function ownText(el) {
        let s = "";
        for (const n of el.childNodes) {
            if (n.nodeType === Node.TEXT_NODE) {
                s += n.textContent;
            } else if (n.nodeType === Node.ELEMENT_NODE && inlinable(n) && !isHidden(n)) {
                s += n.textContent;
            }
        }
        return clip(squash(s), o.maxText);
    }

    function shorten(name, v) {
        if ((name === "src" || name === "href") && /^data:/i.test(v))
            return v.slice(0, 24) + "…";
        return clip(v, o.maxAttr);
    }

    function attrString(el) {
        const tag = el.localName;
        const attrs = new Map();

        for (const a of el.attributes) {
            const n = a.name;
            if (n === "class") {
                const cls = a.value.split(/\s+/)
                    .filter(t => t && !GENERATED.test(t))
                    .slice(0, o.maxClasses)
                    .join(" ");
                if (cls) attrs.set(n, cls);
            } else if (KEEP.has(n) || n.startsWith("aria-") || DATA_KEEP.test(n)) {
                attrs.set(n, a.value);
            }
        }

        // Attributes only hold the initial HTML; read the live state instead.
        if (tag === "input") {
            if (el.type === "checkbox" || el.type === "radio") {
                if (el.checked) attrs.set("checked", "");
                else attrs.delete("checked");
            } else if (el.type === "password" || el.type === "file") {
                attrs.delete("value");            // never leak secrets
            } else if (el.value) {
                attrs.set("value", el.value);
            } else {
                attrs.delete("value");
            }
        } else if (tag === "textarea" || tag === "select") {
            if (el.value) attrs.set("value", el.value);
            else attrs.delete("value");
        } else if (tag === "option") {
            if (el.selected) attrs.set("selected", "");
            else attrs.delete("selected");
        }

        const out = [];
        for (const [n, v] of attrs) {
            if (BOOL.has(n) && (v === "" || v === n || v === "true")) out.push(n);
            else if (v !== "") out.push(`${n}=${JSON.stringify(shorten(n, v))}`);
        }
        return out.join(" ");
    }

    // Tag + class (minus state classes) for the node and its first children,
    // so one "active" item doesn't split a run of otherwise identical items.
    const ident = el =>
        el.localName + "." +
        (el.getAttribute("class") || "")
            .split(/\s+/).filter(t => t && !STATE.test(t)).sort().join(".");

    const sig = el =>
        ident(el) + "|" + [...el.children].slice(0, 6).map(ident).join(",");

    // Rows like <div class="form-group"> look identical but each holds a different input.
    const hasFields = el => el.matches(EDITABLE) || !!el.querySelector(EDITABLE);

    function walkChildren(kids, depth) {
        const indent = "  ".repeat(depth);
        const sigs = kids.map(sig);

        for (let i = 0; i < kids.length;) {
            let j = i + 1;
            while (j < kids.length && sigs[j] === sigs[i]) j++;

            const count = j - i;
            const size = kids[i].querySelectorAll("*").length;
            // Complex items collapse from 4 repeats; trivial ones only when huge.
            const collapse =
                o.sampleRepeated &&
                !hasFields(kids[i]) &&
                ((count >= 4 && size >= 5) || count >= 25);

            if (collapse) {
                const cls = k => kids[k].getAttribute("class") || "";
                let skipped = 0;
                const flush = () => {
                    if (skipped) lines.push(`${indent}[... ${skipped} omitted ...]`);
                    skipped = 0;
                };

                lines.push(`${indent}[REPEATED ${count} TIMES]`);
                walk(kids[i], depth);
                walk(kids[i + 1], depth);
                for (let k = i + 2; k < j - 1; k++) {
                    // Items that differ in class (active/selected/...) stay visible.
                    if (cls(k) !== cls(i) ||
                        kids[k].matches("[aria-selected=true],[aria-current],[aria-checked=true]")) {
                        flush();
                        walk(kids[k], depth);
                    } else {
                        skipped++;
                    }
                }
                flush();
                walk(kids[j - 1], depth);
            } else {
                for (let k = i; k < j; k++) walk(kids[k], depth);
            }
            i = j;
        }
    }

    function walk(el, depth) {
        if (lines.length >= o.maxLines) { truncated = true; return; }

        const tag = el.localName;
        const kids = kidsOf(el);
        const attrs = attrString(el);
        const text = tag === "textarea" ? "" : ownText(el);

        // Flatten <div><div><div>…</div></div></div> chains and drop empty ones.
        if (o.collapseWrappers && WRAPPERS.has(tag) && !attrs && !text) {
            if (kids.length === 0) return;
            if (kids.length === 1) return walk(kids[0], depth);
        }

        const indent = "  ".repeat(depth);
        lines.push(`${indent}<${tag}${attrs ? " " + attrs : ""}>` + (text ? ` ${text}` : ""));

        if (!kids.length) return;
        if (depth >= o.maxDepth) {
            lines.push(`${indent}  [... ${kids.length} children not shown (depth limit) ...]`);
            return;
        }
        walkChildren(kids, depth + 1);
    }

    walk(root, 0);
    if (truncated) lines.push(`[... output truncated at ${o.maxLines} lines ...]`);
    return lines.join("\n");
}
"""
