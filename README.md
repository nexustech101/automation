# browser: web scraping guide

`browser` is a single-file library (`browser/browser.py`) for scraping with a real browser. It
starts Brave, Chrome or Edge as a normal app with its own profile, so you stay
signed in between runs. It controls that browser with
[Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright), a stealth fork
of Playwright, and solves CAPTCHAs with
[SeleniumBase](https://github.com/seleniumbase/SeleniumBase).

## Install

Copy the `browser/` folder into your project (or install this repo with
`pip install .`), then:

```bash
pip install patchright seleniumbase
```

You need Python 3.12+ and Brave, Chrome or Edge installed. You don't need
`patchright install`, because the library drives the browser you already have.
SeleniumBase is only loaded when a CAPTCHA has to be solved.

## Quick start

```python
import asyncio
import browser

async def main():
    async with browser.async_browser_session("brave") as session:
        await browser.open_page(session.page, "https://books.toscrape.com/")
        print(await session.page.title())

asyncio.run(main())
```

`async_browser_session` starts the browser, or reuses one this library already
has running. When the block ends it disconnects, and it closes the browser
only if this block launched it. Pass `keep_open=True` to leave the browser
running so the next run starts faster.

Always navigate with `browser.open_page(page, url)` rather than `page.goto(url)`.
It also solves any CAPTCHA on the page and raises an error on HTTP 4xx/5xx responses.

## Scrape one page

Once the page is open, use the normal Playwright API:

```python
async with browser.async_browser_session() as session:
    page = session.page
    await browser.open_page(page, "https://books.toscrape.com/", site="Books")

    books = await page.evaluate("""() =>
        [...document.querySelectorAll("article.product_pod")].map(card => ({
            title: card.querySelector("h3 a").title,
            price: card.querySelector(".price_color").textContent,
            url: card.querySelector("h3 a").href,
        }))
    """)
```

`site="Books"` only names the site in error messages, e.g.
`Books returned HTTP 404 at ...`.

Tip: Patchright runs `evaluate` in an isolated world, so it can't see the page's
own JavaScript variables. To read one, such as `window.__INITIAL_STATE__`, pass
`isolated_context=False`:

```python
state = await page.evaluate("() => window.__INITIAL_STATE__", isolated_context=False)
```

## Scrape many pages in parallel

`scrape_pages` opens each URL in its own tab, runs your extractor on it, and
returns the results in the same order as the URLs. Each tab goes through
`open_page`, so CAPTCHAs are solved and HTTP errors are caught.

```python
async def extract_books(tab, url):
    return await tab.eval_on_selector_all(
        "article.product_pod h3 a", "links => links.map(a => a.title)"
    )

urls = [browser.build_paginated_url("https://shop.example.com/search?q=lamp", n) for n in range(1, 6)]
# -> https://shop.example.com/search?q=lamp&page=1 ... &page=5

async with browser.async_browser_session() as session:
    pages = await browser.scrape_pages(
        session.context,
        urls,
        extract_books,
        site="Shop",
        max_concurrent_tabs=3,  # tabs open at once
        load_wait=2.0,          # seconds to wait after load, for JS-rendered content
    )
    titles = [title for page in pages for title in page]
```

The extractor receives `(tab, url)` and can return anything. Tabs are always
closed afterwards.

When a page fails:

| `on_error=` | Behavior |
|---|---|
| `"raise"` (default) | Stops the remaining tabs and raises `RuntimeError("Shop scraping failed: ...")`. Nothing is silently lost. |
| `"skip"` | Logs a warning for the failed URL and puts `None` in its place, so results still line up with `urls`. |

If the site uses a different query parameter for paging, pass it with
`build_paginated_url(url, 2, param="p")`.

## CAPTCHAs

### Automatic (most scrapers need nothing else)

`open_page` and `scrape_pages` check every page they load. When they find an
unsolved challenge, they run SeleniumBase's `sb.solve_captcha()` on that exact
tab. SeleniumBase connects to the browser that's already running, clicks the
challenge, and disconnects without closing anything.

It recognizes:

- Cloudflare: the "Just a moment..." interstitial and Turnstile widgets
- Google reCAPTCHA (the invisible badge alone doesn't count)
- hCaptcha
- DataDome

If the challenge is solved, the page loads normally. Sites behind these walls
often answer 403/429 before the challenge, so that status is ignored once it's
solved. If it isn't solved within 15 seconds, `open_page` raises
`RuntimeError("... CAPTCHA at <url> was not solved; solve it in the browser and rerun.")`.

### Manual: check and solve at any point

A CAPTCHA can also appear after an action, such as submitting a login form. Use the
functions directly:

```python
await page.click("button[type=submit]")

if await browser.has_captcha(page):              # True while an unsolved challenge is showing
    solved = await browser.solve_captcha(page)   # SeleniumBase solve; True once the page is clear
```

`solve_captcha(page, timeout=30)` waits longer for the challenge to clear.

### Fallback: let a person finish it

SeleniumBase clicks checkbox challenges. It can't solve the image puzzles
that sometimes follow. For those, keep the browser visible and wait for
someone to finish it:

```python
await browser.open_page(page, url, solve_captchas=False)  # check HTTP status only

if await browser.has_captcha(page) and not await browser.solve_captcha(page):
    print("Please complete the CAPTCHA in the browser window...")
    if not await browser.wait_for_captcha_clear(page, timeout=300):
        raise RuntimeError("CAPTCHA was not completed in 5 minutes")
```

### Tips for fewer CAPTCHAs

- Don't use `headless=True` on protected sites. Headless browsers are flagged
  far more often.
- Keep the same profile, which is the default, so cookies and logins build up
  trust over time.
- Lower `max_concurrent_tabs` and raise `load_wait` if a site keeps challenging you.
- CAPTCHA solving needs the async API (`async_browser_session`).

## Settings

Change these once, before you open the first session:

```python
from pathlib import Path
import browser

browser.settings.data_dir = Path("~/.my_scraper").expanduser()    # profiles + state (default ~/.browser_automation)
browser.settings.cdp_port = 9333                                  # Brave's port (default 9222); Chrome +1, Edge +2
browser.settings.executables["brave"] = "D:/Apps/Brave/brave.exe" # if it isn't installed in the usual place
browser.settings.attach_timeout = 10                              # seconds (default 5)
```

The default `data_dir` is shared by every project that uses `browser.py`, so
they all reuse the same signed-in profiles. To keep a project separate, give
it its own `data_dir`.

Each browser has its own default port (Brave 9222, Chrome 9223, Edge 9224),
so asking for one browser never attaches to another that is already running.
When a session that launched the browser ends, the browser is asked to close
cleanly so its profile and logins are saved, and is only killed if it hangs.

## Other ways to connect

**A browser you started yourself.** Start it with a debugging port, then
attach. The library will never launch or close this browser:

```bash
brave --remote-debugging-port=9222 --user-data-dir=C:/temp/scrape-profile
```

```python
async with browser.async_browser_session(cdp_url="http://127.0.0.1:9222") as session:
    ...
```

**Sync code (no asyncio).** Same options, normal Playwright sync API.
`open_page`, `scrape_pages` and the CAPTCHA functions are async-only:

```python
with browser.sync_browser_session("chrome", headless=True) as session:
    session.page.goto("https://example.com")
    print(session.page.title())
```

## Feed a page to an LLM

`minimize_dom` turns the page into a short, indented outline that an LLM can
read cheaply. It keeps text, links, form fields and ARIA attributes, and leaves
out scripts, styles and hidden elements. Password values are never included.

```python
outline = await browser.minimize_dom(page, sample_repeated=True, max_lines=500)
```

`sample_repeated=True` collapses long lists of identical items, such as 50
product cards, into a few examples.

## Troubleshooting

| Error | What to do |
|---|---|
| `Could not find 'brave' on Windows` | Install it, pick another browser, or set `settings.executables["brave"]`. |
| `The browser exited before exposing CDP` | That profile is already open in another window of the same browser. Close it, or use a different `data_dir`. |
| `Could not attach to browser CDP at ...` | The browser didn't respond. The library already retried once on a fresh profile, so close stray browser windows and rerun. |
| `... CAPTCHA at ... was not solved` | Use the [person-finishes-it fallback](#fallback-let-a-person-finish-it), or retry without `headless`. |
| `solve_captcha() needs SeleniumBase` | `pip install seleniumbase` |
| `<site> returned HTTP 404 at ...` | The URL is wrong or the page is gone. Use `on_error="skip"` to continue past it. |

## API at a glance

| Function | Purpose |
|---|---|
| `async_browser_session(browser="brave", *, cdp_url, headless, keep_open)` | Start or attach a browser for an `async with` block |
| `sync_browser_session(...)` / `start_browser(...)` | The same for sync code |
| `open_page(page, url, *, site, wait_until, timeout, solve_captchas=True)` | Navigate, solve any CAPTCHA, reject HTTP errors |
| `scrape_pages(context, urls, extractor, *, site, max_concurrent_tabs=5, load_wait=0, on_error="raise")` | Many URLs in parallel tabs, results in order |
| `build_paginated_url(url, n, *, param="page")` | Set `page=n` in a URL's query |
| `has_captcha(page)` | Is an unsolved CAPTCHA showing? |
| `solve_captcha(page, *, timeout=15)` | Solve it with SeleniumBase; `True` when the page is clear |
| `wait_for_captcha_clear(page, timeout)` | Wait for a person to solve it |
| `minimize_dom(page, ...)` | Compact page outline for an LLM |
| `settings` | Ports, profile folder, browser paths, timeouts |
| `find_browser`, `start_cdp`, `launch_browser`, `cdp_is_up`, `wait_for_cdp` | Lower-level browser control |
