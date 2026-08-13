#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone email extractor for a single web page you provide.

Handles emails that are hidden behind an "email icon"/logo, including:
  * mailto: links and bare href="name@domain" links
  * Cloudflare-obfuscated emails (data-cfemail / "email-protection" links)
  * screen-reader-only spans (e.g. Finalsite .fsStyleSROnly)
  * "name (at) domain (dot) org" style text obfuscation
  * emails injected by JavaScript, or only revealed after clicking the icon

It renders the page in a real browser (so JS-revealed emails appear), clicks
anything that looks like an email icon to force the address to show, then
extracts every address it can find (with a nearby name/title when possible).

------------------------------------------------------------------------------
COLAB SETUP (run these on SEPARATE lines, once per session):

    !pip install -q playwright nest_asyncio beautifulsoup4 lxml pandas
    !playwright install chromium
    !playwright install-deps chromium

USAGE:

    import nest_asyncio; nest_asyncio.apply()
    from email_extractor import get_emails

    df = get_emails("https://www.example-school.org/staff-directory")
    df                      # a table: name | title | email | source
    df.to_csv("emails.csv", index=False)

    # options:
    #   click_icons=True   click email icons to reveal (default True)
    #   wait_ms=2500       how long to let JS run after load
    #   paginate=True      follow ?page / "Next" pagination (default True)
    #   headless=True      set False only if debugging locally
------------------------------------------------------------------------------
"""

from __future__ import annotations

import re
import asyncio
from typing import Dict, List, Optional, Set, Tuple

from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# "name (at) domain (dot) org"  /  "name [at] domain dot org" etc.
AT_DOT_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*(?:\(|\[|\{)?\s*(?:at|@)\s*(?:\)|\]|\})?\s*"
    r"([a-zA-Z0-9.\-]+(?:\s*(?:\(|\[|\{)?\s*(?:dot|\.)\s*(?:\)|\]|\})?\s*"
    r"[a-zA-Z0-9.\-]+)+)", re.IGNORECASE)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_BAD_DOMAINS = ("example.com", "sentry.io", "wixpress.com", "schema.org",
                "googleapis.com", "gstatic.com", "w3.org")


# ---------------------------------------------------------------------------
# decoders
# ---------------------------------------------------------------------------

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _valid(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    email = email.strip().strip(".,;:").lower()
    if not EMAIL_RE.fullmatch(email):
        return None
    if any(bad in email for bad in _BAD_DOMAINS):
        return None
    if email.endswith((".png", ".jpg", ".gif", ".svg", ".webp")):
        return None
    return email


def decode_cfemail(hexstr: str) -> Optional[str]:
    """Decode a Cloudflare data-cfemail hex string to a real address."""
    try:
        r = int(hexstr[:2], 16)
        out = "".join(
            chr(int(hexstr[i:i + 2], 16) ^ r)
            for i in range(2, len(hexstr), 2))
        return _valid(out)
    except Exception:
        return None


def decode_at_dot(text: str) -> List[str]:
    """Turn 'name at domain dot org' into name@domain.org."""
    found = []
    for m in AT_DOT_RE.finditer(text or ""):
        local = m.group(1)
        domain = re.sub(r"\s*(?:\(|\[|\{)?\s*(?:dot|\.)\s*(?:\)|\]|\})?\s*",
                        ".", m.group(2), flags=re.IGNORECASE)
        domain = re.sub(r"\s+", "", domain)
        cand = _valid(f"{local}@{domain}")
        if cand:
            found.append(cand)
    return found


# ---------------------------------------------------------------------------
# extraction from rendered HTML
# ---------------------------------------------------------------------------

def emails_from_html(html: str) -> List[Tuple[str, str, str, str]]:
    """Return list of (name, title, email, source) found in the HTML."""
    soup = BeautifulSoup(html, "lxml")
    results: List[Tuple[str, str, str, str]] = []
    seen: Set[str] = set()

    def add(email, name="", title="", source=""):
        e = _valid(email)
        if e and e not in seen:
            seen.add(e)
            results.append((_clean(name), _clean(title), e, source))

    # 1) mailto: links (name/title from link context)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            add(href[7:].split("?")[0], a.get_text(), "", "mailto")
        # 1b) bare href that is actually an email (Finalsite style)
        elif "@" in href and "/" not in href and " " not in href:
            add(href, a.get_text(), "", "href")

    # 2) Cloudflare-protected emails
    for el in soup.select("[data-cfemail]"):
        dec = decode_cfemail(el.get("data-cfemail", ""))
        if dec:
            add(dec, "", "", "cloudflare")
    for a in soup.select("a[href*='email-protection']"):
        m = re.search(r"#([0-9a-fA-F]+)$", a.get("href", ""))
        if m:
            dec = decode_cfemail(m.group(1))
            if dec:
                add(dec, "", "", "cloudflare")

    # 3) screen-reader-only / explicit email spans
    for el in soup.select(".fsStyleSROnly, .fsEmail, [class*='email' i], "
                          "[class*='mail' i]"):
        m = EMAIL_RE.search(el.get_text(" "))
        if m:
            add(m.group(0), "", "", "span")

    # 4) "at / dot" obfuscated text
    for e in decode_at_dot(soup.get_text(" ")):
        add(e, "", "", "at-dot")

    # 5) any remaining plain-text emails
    for m in EMAIL_RE.finditer(soup.get_text(" ")):
        add(m.group(0), "", "", "text")

    return results


# ---------------------------------------------------------------------------
# browser rendering + icon clicking
# ---------------------------------------------------------------------------

# things that look like an email icon / trigger
_ICON_SELECTORS = [
    "a[href^='mailto:']", "[class*='email' i]", "[class*='mail' i]",
    "[class*='envelope' i]", "i.fa-envelope", ".fa-envelope",
    "[aria-label*='email' i]", "[title*='email' i]",
    "svg[class*='mail' i]", "button[class*='mail' i]",
]

_NEXT_SELECTORS = [
    "a[rel='next']", "a.fsNextPageLink", "a[aria-label*='Next' i]",
    ".pagination a.next", "li.next a", ".fsElementPagination a[rel='next']",
]


async def _render_and_collect(url: str, click_icons: bool, wait_ms: int,
                              paginate: bool, headless: bool, max_pages: int
                              ) -> List[Tuple[str, str, str, str]]:
    from playwright.async_api import async_playwright

    all_rows: List[Tuple[str, str, str, str]] = []
    seen: Set[str] = set()

    def merge(rows):
        for name, title, email, source in rows:
            if email not in seen:
                seen.add(email)
                all_rows.append((name, title, email, source))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        ctx = await browser.new_context(user_agent=_UA,
                                        ignore_https_errors=True)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print("Could not load page:", exc)
            await browser.close()
            return all_rows

        for _ in range(max_pages if paginate else 1):
            await page.wait_for_timeout(wait_ms)
            try:
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            await page.wait_for_timeout(400)

            # read what's already there
            merge(emails_from_html(await page.content()))

            # click email icons to reveal hidden addresses
            if click_icons:
                for sel in _ICON_SELECTORS:
                    try:
                        handles = await page.query_selector_all(sel)
                    except Exception:
                        handles = []
                    for h in handles[:400]:
                        try:
                            if await h.is_visible():
                                await h.click(timeout=800)
                                await page.wait_for_timeout(120)
                        except Exception:
                            pass
                    # re-read after this selector's clicks
                    merge(emails_from_html(await page.content()))
                # close any popup/modal that may have opened
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

            # pagination
            if not paginate:
                break
            nxt = None
            for sel in _NEXT_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    try:
                        if await el.is_visible():
                            nxt = el
                            break
                    except Exception:
                        pass
            if not nxt:
                break
            try:
                await nxt.click()
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                await page.wait_for_timeout(1000)

        await browser.close()
    return all_rows


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def get_emails(url: str, click_icons: bool = True, wait_ms: int = 2500,
               paginate: bool = True, headless: bool = True,
               max_pages: int = 40):
    """Extract emails from a single page. Returns a pandas DataFrame with
    columns: name, title, email, source. Requires nest_asyncio.apply() to have
    been called in the notebook."""
    try:
        loop = asyncio.get_event_loop()
        rows = loop.run_until_complete(
            _render_and_collect(url, click_icons, wait_ms, paginate,
                                headless, max_pages))
    except RuntimeError as exc:
        raise SystemExit(
            "Async loop error — run `import nest_asyncio; nest_asyncio.apply()`"
            " first.\n" + str(exc))

    try:
        import pandas as pd
        df = pd.DataFrame(rows, columns=["name", "title", "email", "source"])
        print(f"\nFound {len(df)} unique emails at {url}")
        return df
    except Exception:
        for r in rows:
            print(r)
        return rows


# Static-only fallback (no browser) — for pages whose emails are already in the
# HTML (Cloudflare/at-dot/mailto). Faster, but won't get click/JS-revealed ones.
def get_emails_static(url: str):
    import requests
    import pandas as pd
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass
    r = requests.get(url, headers={"User-Agent": _UA}, verify=False,
                     timeout=20)
    rows = emails_from_html(r.text)
    df = pd.DataFrame(rows, columns=["name", "title", "email", "source"])
    print(f"Found {len(df)} emails (static) at {url}")
    return df


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for row in get_emails_static(sys.argv[1]).itertuples(index=False):
            print(row)
