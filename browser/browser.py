"""
Stealth browser automation and scraping over CDP, in one file.

The browser (Brave, Chrome or Edge) runs as an ordinary process with its own
automation profile, so logins persist between runs. Patchright attaches to it
over the Chrome DevTools Protocol, which means sites see a normal browser
instead of a driver-launched one. SeleniumBase solves CAPTCHAs on the same
tabs, and scrape_pages() reads many pages in parallel tabs.

Requires Python 3.12+ and:
    pip install patchright        # required
    pip install seleniumbase      # only for solve_captcha()

Public API, in file order (see README.md for a guide):
    Settings      settings, Settings
    Sessions      async_browser_session, sync_browser_session, start_browser
    Browsers      start_cdp, launch_browser, cdp_is_up, wait_for_cdp, find_browser
    Scraping      open_page, scrape_pages, build_paginated_url
    CAPTCHA       has_captcha, solve_captcha, wait_for_captcha_clear  (SeleniumBase)
    LLM outline   minimize_dom

open_page() and scrape_pages() detect and solve CAPTCHAs automatically.

*Usage:*
>>> import asyncio
>>> import browser
>>> async def main():
...     async with browser.async_browser_session("brave") as session:
...         await browser.open_page(session.page, "https://example.com")
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
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator, Iterator, Literal, TypeVar

from patchright.async_api import (
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Error as PlaywrightError,
    Page as AsyncPage,
    Playwright as AsyncPlaywright,
    Response as AsyncResponse,
    async_playwright,
)
from patchright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

__all__ = [
    "AsyncBrowserSession",
    "BrowserSession",
    "CdpServer",
    "Settings",
    "async_browser_session",
    "build_paginated_url",
    "cdp_is_up",
    "find_browser",
    "has_captcha",
    "launch_browser",
    "minimize_dom",
    "open_page",
    "scrape_pages",
    "settings",
    "solve_captcha",
    "start_browser",
    "start_cdp",
    "sync_browser_session",
    "wait_for_captcha_clear",
    "wait_for_cdp",
]

logger = logging.getLogger(__name__)

BrowserName = Literal["brave", "chrome", "edge"]
SUPPORTED_BROWSERS: tuple[BrowserName, ...] = ("brave", "chrome", "edge")
WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


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
    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"

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
    A sync Patchright handle on a running browser. close() detaches and
    stops the browser only when asked to and only if this session launched it.

    *Usage:*
    >>> session = start_browser("brave")
    >>> session.page.goto("https://example.com")
    >>> session.close()
    """

    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    cdp_url: str
    process: subprocess.Popen | None = None

    def close(self, terminate_process: bool = False) -> None:
        """
        Detach. With terminate_process, a browser this session launched is
        asked to shut down cleanly first, so its profile and logins are saved.

        *Usage:*
        >>> session.close(terminate_process=True)
        """
        stop_browser = terminate_process and self.process is not None
        try:
            if stop_browser:
                with suppress(Exception):  # The connection drops as the browser exits.
                    self.browser.new_browser_cdp_session().send("Browser.close")
            self.playwright.stop()
        finally:
            if stop_browser:
                _stop_process(self.process)


@dataclass
class AsyncBrowserSession:
    """
    An async Patchright handle on a running browser. Usually obtained from
    async_browser_session(), which closes it for you.

    *Usage:*
    >>> async with async_browser_session() as session:
    ...     await session.page.goto("https://example.com")
    """

    playwright: AsyncPlaywright
    browser: AsyncBrowser
    context: AsyncBrowserContext
    page: AsyncPage
    cdp_url: str
    process: subprocess.Popen | None = None

    async def close(self, terminate_process: bool = False) -> None:
        """
        Detach. With terminate_process, a browser this session launched is
        asked to shut down cleanly first, so its profile and logins are saved.

        *Usage:*
        >>> await session.close(terminate_process=True)
        """
        stop_browser = terminate_process and self.process is not None
        try:
            if stop_browser:
                with suppress(Exception):  # The connection drops as the browser exits.
                    cdp = await self.browser.new_browser_cdp_session()
                    await cdp.send("Browser.close")
            await self.playwright.stop()
        finally:
            if stop_browser:
                await asyncio.to_thread(_stop_process, self.process)


def _stop_process(process: subprocess.Popen, timeout: float = _SHUTDOWN_TIMEOUT) -> None:
    """Wait for a browser that was asked to close; kill it only if it hangs."""
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout)


@contextmanager
def sync_browser_session(
    browser: BrowserName = "brave",
    *,
    cdp_url: str | None = None,
    headless: bool = False,
    keep_open: bool = False,
) -> Generator[BrowserSession]:
    """
    Start or attach a browser for the duration of the block. On exit it
    detaches, and stops a browser it launched unless keep_open is set.

    *Usage:*
    >>> with sync_browser_session("brave", keep_open=True) as session:
    ...     session.page.goto("https://example.com")
    """
    session = start_browser(browser, cdp_url=cdp_url, headless=headless)
    try:
        yield session
    finally:
        session.close(terminate_process=not keep_open)


@asynccontextmanager
async def async_browser_session(
    browser: BrowserName = "brave",
    *,
    cdp_url: str | None = None,
    headless: bool = False,
    keep_open: bool = False,
) -> AsyncGenerator[AsyncBrowserSession]:
    """
    Start or attach a browser for the duration of the block. Without cdp_url
    it reuses the browser's running automation instance or launches one; if
    attaching fails, it retries once on a fresh port and profile, which may
    need signing in again. With cdp_url it only attaches and never launches.
    session.cdp_url is the endpoint actually used.

    *Usage:*
    >>> async with async_browser_session("brave") as session:
    ...     await session.page.goto("https://example.com")
    >>> async with async_browser_session(cdp_url="http://127.0.0.1:9222") as session:
    ...     print(session.page.url)
    """
    if cdp_url is None:
        session = await _start_async_session(browser, headless)
    else:
        session = await _attach_async_existing(cdp_url)
    try:
        yield session
    finally:
        # Room for a clean shutdown, then the forced one.
        async with asyncio.timeout(_SHUTDOWN_TIMEOUT * 3):
            await session.close(terminate_process=not keep_open)


def start_browser(
    browser: BrowserName = "brave",
    *,
    cdp_url: str | None = None,
    headless: bool = False,
) -> BrowserSession:
    """
    The sync counterpart of async_browser_session() for callers that manage
    closing themselves. Prefer sync_browser_session() in a with block.

    *Usage:*
    >>> session = start_browser("brave", headless=True)
    >>> session.page.goto("https://example.com")
    >>> session.close(terminate_process=True)
    """
    if cdp_url is not None:
        wait_for_cdp(cdp_url)
        try:
            return _attach_sync(cdp_url)
        except Exception as exc:
            raise _attach_error(cdp_url, exc) from exc

    for fresh in _START_ATTEMPTS:
        server = start_cdp(browser, headless=headless, fresh=fresh)
        try:
            session = _attach_sync(server.url)
        except BaseException as exc:
            _abandon_attempt(server, exc, last_attempt=fresh)
            continue
        return _adopt_server(session, browser, server)
    raise AssertionError("unreachable: the fresh attempt returns or raises")


async def _start_async_session(browser: BrowserName, headless: bool) -> AsyncBrowserSession:
    for fresh in _START_ATTEMPTS:
        server = await asyncio.to_thread(start_cdp, browser, headless=headless, fresh=fresh)
        try:
            session = await _attach_async(server.url)
        except BaseException as exc:
            _abandon_attempt(server, exc, last_attempt=fresh)
            continue
        return _adopt_server(session, browser, server)
    raise AssertionError("unreachable: the fresh attempt returns or raises")


# Reuse or launch on the usual port first, then retry on a fresh port and profile.
_START_ATTEMPTS = (False, True)

_Session = TypeVar("_Session", BrowserSession, AsyncBrowserSession)


def _abandon_attempt(server: CdpServer, exc: BaseException, *, last_attempt: bool) -> None:
    """Stop what the attempt launched; re-raise unless another attempt is left."""
    server.terminate()
    if not isinstance(exc, Exception):
        raise exc
    if last_attempt:
        raise _attach_error(server.url, exc) from exc


def _adopt_server(session: _Session, browser: BrowserName, server: CdpServer) -> _Session:
    """Saved only after Patchright has attached, so a broken port is never reused."""
    _remember_port(browser, server.port)
    session.process = server.process
    return session


async def _attach_async_existing(cdp_url: str) -> AsyncBrowserSession:
    await asyncio.to_thread(wait_for_cdp, cdp_url)
    try:
        return await _attach_async(cdp_url)
    except Exception as exc:
        raise _attach_error(cdp_url, exc) from exc


def _attach_sync(endpoint: str) -> BrowserSession:
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=settings.attach_timeout * 1000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
    except BaseException:
        with suppress(Exception):
            playwright.stop()
        raise
    return BrowserSession(playwright, browser, context, page, cdp_url=endpoint)


async def _attach_async(endpoint: str) -> AsyncBrowserSession:
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
    _ENDPOINTS[browser] = endpoint
    return AsyncBrowserSession(playwright, browser, context, page, cdp_url=endpoint)


def _attach_error(endpoint: str, cause: Exception) -> RuntimeError:
    return RuntimeError(f"Could not attach to browser CDP at {endpoint}: {cause}")


# The endpoint each async session attached through, so solve_captcha(page)
# can point SeleniumBase at the same browser without the caller passing it.
_ENDPOINTS: weakref.WeakKeyDictionary[AsyncBrowser, str] = weakref.WeakKeyDictionary()


# --------------------------------------------------------------------------
# Browser processes and CDP endpoints
# --------------------------------------------------------------------------


@dataclass
class CdpServer:
    """A CDP endpoint, plus its process when this module launched it."""

    port: int
    process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://{settings.cdp_host}:{self.port}"

    def terminate(self) -> None:
        if self.process is not None:
            self.process.terminate()


def start_cdp(browser: BrowserName = "brave", *, headless: bool = False, fresh: bool = False) -> CdpServer:
    """
    Reuse the browser's last working endpoint or the default port if one is
    live, otherwise launch the browser. fresh=True always launches on a new
    port with its own profile, for recovering from a stuck instance.

    *Usage:*
    >>> server = start_cdp("brave")
    >>> server.url
    'http://127.0.0.1:9222'
    """
    _require_supported(browser)
    with _START_LOCK:
        if fresh:
            port = _free_port()
        else:
            port = _remembered_port(browser)
            for live_port in dict.fromkeys((port, settings.default_port(browser))):
                if cdp_is_up(CdpServer(live_port).url):
                    return CdpServer(live_port)
        return CdpServer(port, launch_browser(browser, headless=headless, port=port))


def launch_browser(browser: BrowserName = "brave", *, headless: bool = False, port: int | None = None) -> subprocess.Popen:
    """
    Launch the browser with its automation profile and wait for CDP. The
    browser's default port (settings.default_port) uses its main profile;
    other ports get their own. If CDP never comes up, the launched process
    is terminated.

    *Usage:*
    >>> process = launch_browser("chrome", headless=True, port=9333)
    >>> process.terminate()
    """
    port = port or settings.default_port(browser)
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
        wait_for_cdp(CdpServer(port).url, timeout=settings.launch_timeout, process=process)
    except BaseException:
        process.terminate()
        raise
    return process


def cdp_is_up(cdp_url: str | None = None) -> bool:
    """*Usage:*
    >>> cdp_is_up("http://127.0.0.1:9222")
    True
    """
    try:
        with _DIRECT_OPENER.open(f"{(cdp_url or settings.cdp_url).rstrip('/')}/json/version", timeout=0.5) as response:
            data = json.load(response)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("webSocketDebuggerUrl"))


def wait_for_cdp(
    cdp_url: str | None = None,
    *,
    timeout: float = 15.0,
    process: subprocess.Popen | None = None,
) -> None:
    """
    Block until the endpoint answers. Fails early if `process` exits first,
    which usually means its profile is already open in another instance.

    *Usage:*
    >>> wait_for_cdp("http://127.0.0.1:9222", timeout=30)
    """
    endpoint = cdp_url or settings.cdp_url
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp_is_up(endpoint):
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError("The browser exited before exposing CDP. Its automation profile may already be in use.")
        time.sleep(0.2)
    raise TimeoutError(f"Browser CDP endpoint never came up at {endpoint}")


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


def _remember_port(browser: str, port: int) -> None:
    settings.profile_root.mkdir(parents=True, exist_ok=True)
    _port_file(browser).write_text(json.dumps({"port": port}), encoding="utf-8")


def _port_file(browser: str) -> Path:
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
# Navigation and scraping
# --------------------------------------------------------------------------


async def open_page(
    page: AsyncPage,
    url: str,
    *,
    site: str = "The site",
    wait_until: WaitUntil = "domcontentloaded",
    timeout: float | None = None,
    solve_captchas: bool = True,
) -> AsyncResponse | None:
    """
    Navigate, solve any CAPTCHA, then reject HTTP errors. Anti-bot walls
    (Cloudflare, DataDome) answer 403/429 before the real page loads, so the
    status check is skipped once a challenge has been solved. `site` names
    the site in error messages; `timeout` is in seconds.

    *Usage:*
    >>> await open_page(page, "https://www.indeed.com/jobs?q=engineer", site="Indeed")
    >>> await open_page(page, url, solve_captchas=False)  # status check only
    """
    timeout_ms = None if timeout is None else timeout * 1000  # 0 means no timeout
    response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    if solve_captchas and await has_captcha(page):
        if not await solve_captcha(page):
            raise RuntimeError(f"{site} CAPTCHA at {page.url} was not solved; solve it in the browser and rerun.")
        return response
    if response is not None and response.status >= 400:
        raise RuntimeError(f"{site} returned HTTP {response.status} at {page.url}.")
    return response


Extracted = TypeVar("Extracted")


async def scrape_pages(
    context: AsyncBrowserContext,
    urls: list[str],
    extractor: Callable[[AsyncPage, str], Awaitable[Extracted]],
    *,
    site: str = "The site",
    max_concurrent_tabs: int = 5,
    load_wait: float = 0.0,
    on_error: Literal["raise", "skip"] = "raise",
) -> list[Extracted | None]:
    """
    Open each url in its own tab through open_page(), wait `load_wait`
    seconds, and return `extractor(tab, url)` results in url order, one per
    url. Tabs are always closed. on_error="raise" cancels the remaining tabs
    at the first failure; on_error="skip" logs each failed url and puts None
    in its place, so results still line up with urls.

    *Usage:*
    >>> async def title(tab, url):
    ...     return await tab.title()
    >>> await scrape_pages(session.context, urls, title, site="Shop", max_concurrent_tabs=3)
    ['Page one', 'Page two']
    >>> await scrape_pages(session.context, urls, title, on_error="skip")
    ['Page one', None]
    """
    if max_concurrent_tabs < 1 or load_wait < 0:
        raise ValueError("max_concurrent_tabs must be at least 1 and load_wait 0 or more.")
    semaphore = asyncio.Semaphore(max_concurrent_tabs)

    async def scrape(url: str) -> Extracted:
        async with semaphore:
            tab = await context.new_page()
            try:
                await open_page(tab, url, site=site)
                await asyncio.sleep(load_wait)
                return await extractor(tab, url)
            finally:
                await tab.close()

    if on_error == "skip":
        return await _gather_skipping_failures(urls, scrape)
    return await _gather_failing_fast(urls, scrape, site)


async def _gather_failing_fast(
    urls: list[str], scrape: Callable[[str], Awaitable[Extracted]], site: str,
) -> list[Extracted]:
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(scrape(url)) for url in urls]
    except ExceptionGroup as exc:
        raise RuntimeError(f"{site} scraping failed: {exc.exceptions[0]}") from exc
    return [task.result() for task in tasks]


async def _gather_skipping_failures(
    urls: list[str], scrape: Callable[[str], Awaitable[Extracted]],
) -> list[Extracted | None]:
    outcomes = await asyncio.gather(*(scrape(url) for url in urls), return_exceptions=True)
    results: list[Extracted | None] = []
    for url, outcome in zip(urls, outcomes):
        if isinstance(outcome, Exception):
            logger.warning("Skipping %s: %s", url, outcome)
            results.append(None)
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            results.append(outcome)
    return results


def build_paginated_url(base_url: str, page_num: int, *, param: str = "page") -> str:
    """
    Set the page parameter in a URL's query, replacing any existing value
    and keeping the other parameters and the #fragment.

    *Usage:*
    >>> build_paginated_url("https://example.com/search?q=x", 2)
    'https://example.com/search?q=x&page=2'
    >>> build_paginated_url("https://example.com/search?page=1#top", 3)
    'https://example.com/search?page=3#top'
    >>> build_paginated_url("https://example.com/search", 3, param="p")
    'https://example.com/search?p=3'
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


async def has_captcha(page: AsyncPage) -> bool:
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


async def solve_captcha(page: AsyncPage, *, cdp_url: str | None = None, timeout: float = 15.0) -> bool:
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
    host, port = _parse_cdp_endpoint(cdp_url or _ENDPOINTS.get(page.context.browser, settings.cdp_url))
    target_id = await _target_id(page)
    await asyncio.to_thread(_seleniumbase_solve, host, port, target_id)
    return await wait_for_captcha_clear(page, timeout)


async def wait_for_captcha_clear(page: AsyncPage, timeout: float) -> bool:
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


async def _target_id(page: AsyncPage) -> str:
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


def minimize_dom(
    page: Page | AsyncPage,
    *,
    sample_repeated: bool = False,
    max_depth: int = 18,
    max_text: int = 150,
    max_attr: int = 200,
    max_classes: int = 6,
    max_lines: int = 1500,
    keep_hidden: bool = False,
    collapse_wrappers: bool = True,
) -> str | Awaitable[str]:
    """
    A compact, indented outline of <body> for an LLM to read, built in one
    evaluate() call. It keeps ARIA and selector-friendly attributes and live
    form values, and drops scripts, styles and hidden elements. Password and
    file input values are never included, but it is not a general redaction
    tool. sample_repeated collapses long runs of identical siblings (form
    fields are never collapsed). Returns a string for a sync page and an
    awaitable for an async page.

    *Usage:*
    >>> outline = minimize_dom(sync_page)
    >>> outline = await minimize_dom(async_page, sample_repeated=True)
    """
    return page.locator("body").evaluate(_MINIMIZE_DOM_JS, {
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
