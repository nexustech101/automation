# browser: stealth automation guide

`browser` is a small library (core in `browser/browser.py`) for automating a real browser. It
starts the Brave, Chrome or Edge installed on this machine as a normal app with
a persistent automation profile, so you stay signed in between runs. It controls that browser with
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
        await session.page.goto("https://books.toscrape.com/")
        print(await session.page.title())

asyncio.run(main())
```

`async_browser_session` starts the browser, or reuses one this library already
has running. When the block ends it disconnects, and it closes the browser
only if this block launched it. Pass `keep_open=True` to leave the browser
running so the next run starts faster.

`session.page` and `session.context` are ordinary Patchright objects, so
`goto`, locators and everything else in the Playwright API work as usual.

## Scrape one page

```python
async with browser.async_browser_session() as session:
    page = session.page
    response = await page.goto("https://books.toscrape.com/")
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status}")

    books = await page.evaluate("""() =>
        [...document.querySelectorAll("article.product_pod")].map(card => ({
            title: card.querySelector("h3 a").title,
            price: card.querySelector(".price_color").textContent,
            url: card.querySelector("h3 a").href,
        }))
    """)
```

Tip: Patchright runs `evaluate` in an isolated world, so it can't see the page's
own JavaScript variables. To read one, such as `window.__INITIAL_STATE__`, pass
`isolated_context=False`:

```python
state = await page.evaluate("() => window.__INITIAL_STATE__", isolated_context=False)
```

## Run many tabs in parallel

`scrape_pages` opens each URL in its own tab, runs your extractor on it, and
returns the results in the same order as the URLs. Before your extractor runs,
each tab solves any CAPTCHA it lands on and otherwise raises on HTTP 4xx/5xx.

```python
async def extract_books(tab, url):
    await tab.wait_for_selector("article.product_pod")
    return await tab.eval_on_selector_all(
        "article.product_pod h3 a", "links => links.map(a => a.title)"
    )

urls = [f"https://shop.example.com/search?q=lamp&page={n}" for n in range(1, 6)]

async with browser.async_browser_session() as session:
    pages = await browser.scrape_pages(session.context, urls, extract_books, max_concurrent_tabs=3)
    titles = [title for page in pages for title in page]
```

The extractor receives `(tab, url)` and can return anything. Tabs are always
closed afterwards.

When a page fails:

| `on_error=` | Behavior |
|---|---|
| `"raise"` (default) | Stops the remaining tabs and re-raises the first error. Nothing is silently lost. |
| `"skip"` | Logs a warning for the failed URL and puts `None` in its place, so results still line up with `urls`. |

## CAPTCHAs

`scrape_pages` handles CAPTCHAs for you. On a page you navigate yourself,
check after any step that might show one, such as loading a page or
submitting a login form:

```python
await page.goto(url)

if await browser.has_captcha(page):              # True while an unsolved challenge is showing
    solved = await browser.solve_captcha(page)   # SeleniumBase solve; True once the page is clear
```

`solve_captcha` runs SeleniumBase's `sb.solve_captcha()` on that exact tab.
SeleniumBase connects to the browser that's already running, clicks the
challenge, and disconnects without closing anything. `solve_captcha(page, timeout=30)`
waits longer for the challenge to clear.

It recognizes:

- Cloudflare: the "Just a moment..." interstitial and Turnstile widgets
- Google reCAPTCHA (the invisible badge alone doesn't count)
- hCaptcha
- DataDome

Sites behind these walls often answer 403/429 before the challenge, so don't
treat that status as a failure until the challenge is solved.

### Fallback: let a person finish it

SeleniumBase clicks checkbox challenges. It can't solve the image puzzles
that sometimes follow. For those, keep the browser visible and wait for
someone to finish it:

```python
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
- Lower `max_concurrent_tabs` if a site keeps challenging you.

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
Each browser has exactly one profile, `data_dir/profiles/<browser>_profile`.
If it can't be opened the session raises; it never falls back to a fresh,
signed-out profile.
When a session that launched the browser ends, the browser is asked to close
cleanly so its profile and logins are saved, and is only killed if it hangs.

## A browser you started yourself

Start it with a debugging port, then attach. The library will never launch or
close this browser:

```bash
brave --remote-debugging-port=9222 --user-data-dir=C:/temp/scrape-profile
```

```python
async with browser.async_browser_session(cdp_url="http://127.0.0.1:9222") as session:
    ...
```

## Feed a page to an LLM

`minimize_dom` turns the page into a short, indented outline that an LLM can
read cheaply. It keeps text, links, form fields and ARIA attributes, and leaves
out scripts, styles and hidden elements. Password values are never included.

```python
outline = await browser.minimize_dom(page, sample_repeated=True, max_lines=500)
```

It lives in `browser/dom.py` and nothing else depends on it. To drop it, delete
that file and its import line in `browser/__init__.py`.

`sample_repeated=True` collapses long lists of identical items, such as 50
product cards, into a few examples.

## Troubleshooting

| Error | What to do |
|---|---|
| `Could not find 'brave' on Windows` | Install it, pick another browser, or set `settings.executables["brave"]`. |
| `The browser exited before exposing CDP` | That profile is already open in another window of the same browser. Close it, or use a different `data_dir`. |
| `Could not attach to browser CDP at ...` | The browser didn't respond. Close stray windows of the automation browser and rerun, or raise `settings.attach_timeout`. |
| `CAPTCHA at ... was not solved` | Use the [person-finishes-it fallback](#fallback-let-a-person-finish-it), or retry without `headless`. |
| `solve_captcha() needs SeleniumBase` | `pip install seleniumbase` |
| `HTTP 404 at ...` | The URL is wrong or the page is gone. Use `on_error="skip"` to continue past it. |

## API at a glance

| Name | Purpose |
|---|---|
| `async_browser_session(browser="brave", *, cdp_url, headless, keep_open)` | Start or attach a browser for an `async with` block |
| `BrowserSession` | What the block yields: `page`, `context`, `browser`, `cdp_url` |
| `scrape_pages(context, urls, extractor, *, max_concurrent_tabs=5, on_error="raise")` | Many URLs in parallel tabs, results in order |
| `has_captcha(page)` | Is an unsolved CAPTCHA showing? |
| `solve_captcha(page, *, timeout=15)` | Solve it with SeleniumBase; `True` when the page is clear |
| `wait_for_captcha_clear(page, timeout)` | Wait for a person to solve it |
| `minimize_dom(page, ...)` | Compact page outline for an LLM (`browser/dom.py`, optional) |
| `settings`, `find_browser` | Ports, profile folder, browser paths, timeouts |
