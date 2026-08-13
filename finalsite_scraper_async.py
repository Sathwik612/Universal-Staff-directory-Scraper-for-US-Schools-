#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finalsite staff-directory scraper — ASYNC Playwright (Colab-compatible).

Why async: Colab/Jupyter already run an asyncio event loop in the main thread.
Playwright's SYNC api refuses to start there ("Sync API inside the asyncio
loop"). The ASYNC api works with the existing loop when combined with
nest_asyncio. This module uses async Playwright end-to-end.

Also fixes the two things that made earlier renders return nothing:
  * ignore_https_errors=True  (TX school sites have broken certs — the browser
    was silently refusing them, same reason we use verify_ssl=False elsewhere)
  * wait_until="domcontentloaded" (not "networkidle", which never settles on
    ad/tracker-heavy pages and times out)

Finalsite injects staff emails via JS, so we render, let JS run, then read the
emails from the live DOM (mailto, .fsEmail, or .fsStyleSROnly SR-only spans).
Pagination via ?const_page=1..N.

SETUP (Colab), separate lines:
    !pip install -q playwright nest_asyncio
    !playwright install chromium
    !playwright install-deps chromium

Usage (Colab):
    import nest_asyncio, asyncio; nest_asyncio.apply()
    from finalsite_scraper_async import run_finalsite
    df = run_finalsite("all_tx_profiles.csv", output_csv="finalsite_leads.csv",
                       limit=3, concurrency=3)   # test small first

    # single page debug:
    from finalsite_scraper_async import debug_one
    debug_one("https://www.alvaradoisd.net/about/staff-directory")
"""

from __future__ import annotations

import asyncio
import csv
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin

from bs4 import BeautifulSoup

from master_staff_directory_scraper import (
    normalize_name, normalize_email, clean_text, infer_role, classify_lead,
    setup_logging, logger,
)

OUTPUT_FIELDS = ["district", "school", "first_name", "last_name",
                 "email", "role", "department", "lead_type"]
_EMAIL_TEXT_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ---- rendering -------------------------------------------------------------

async def _render(context, url: str, wait_ms: int, timeout_ms: int = 45000
                  ) -> Optional[str]:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(wait_ms)
        return await page.content()
    except Exception as exc:
        logger.debug("render failed %s: %s", url, exc)
        return None
    finally:
        await page.close()


# ---- parsing ---------------------------------------------------------------

def _extract_card_email(card) -> Optional[str]:
    a = card.find("a", href=re.compile(r"^mailto:", re.I))
    if a:
        e = normalize_email(a.get("href"))
        if e:
            return e
    for sel in (".fsEmail", ".fsStyleSROnly", "[class*='mail' i]",
                "[class*='Email' i]"):
        for el in card.select(sel):
            m = _EMAIL_TEXT_RE.search(el.get_text(" "))
            if m:
                e = normalize_email(m.group(0))
                if e:
                    return e
    m = _EMAIL_TEXT_RE.search(card.get_text(" "))
    if m:
        return normalize_email(m.group(0))
    return None


def _extract_page_email(soup) -> Optional[str]:
    """Extract a staff email from a rendered PROFILE page (mailto link, href,
    .fsEmail/.fsStyleSROnly span, or the page's constituent area)."""
    return _extract_profile_details(soup).get("email")


def _extract_profile_details(soup) -> Dict[str, Optional[str]]:
    """Pull email + department + location + title from a rendered profile
    (works for both a full profile page and an in-page modal's HTML)."""
    details: Dict[str, Optional[str]] = {
        "email": None, "department": None, "location": None, "title": None}

    # -- email: mailto, bare-href, .fsEmail/.fsStyleSROnly, then any text --
    for a in soup.find_all("a", href=True):
        href = a["href"]
        e = normalize_email(href[7:] if href.lower().startswith("mailto:")
                            else href)
        if e:
            details["email"] = e
            break
    if not details["email"]:
        for sel in (".fsEmail", ".fsStyleSROnly", ".fsConstituentEmail",
                    "[class*='mail' i]", "[class*='Email' i]"):
            for el in soup.select(sel):
                m = _EMAIL_TEXT_RE.search(el.get_text(" "))
                if m:
                    details["email"] = normalize_email(m.group(0))
                    break
            if details["email"]:
                break
    if not details["email"]:
        region = soup.select_one(
            ".fsConstituentProfile, .fsElementConstituentProfile, "
            ".fsModalContent, .fsProfile, main, #fsPageBody") or soup
        m = _EMAIL_TEXT_RE.search(region.get_text(" "))
        if m:
            details["email"] = normalize_email(m.group(0))

    # -- department / location / title from labelled fields --
    def field(*classes_labels):
        classes, labels = classes_labels
        for c in classes:
            el = soup.select_one(c)
            if el:
                t = clean_text(el.get_text())
                t = re.sub(r"^\s*[\w /&-]{0,20}:\s*", "", t)  # strip "Label:"
                if t and len(t) < 90:
                    return t
        # label-based: find a <strong>Label:</strong> value sibling
        for lab in labels:
            node = soup.find(string=re.compile(rf"{lab}\s*:", re.I))
            if node:
                parent = node.parent
                txt = clean_text(parent.get_text())
                txt = re.sub(rf"^.*?{lab}\s*:\s*", "", txt, flags=re.I)
                if txt and len(txt) < 90:
                    return txt
        return None

    details["department"] = field(
        (".fsDepartments", ".fsConstituentDepartment", "[class*='department' i]"),
        ("Department", "Dept"))
    details["location"] = field(
        (".fsLocations", ".fsBuildings", ".fsConstituentLocation",
         "[class*='location' i]", "[class*='building' i]", "[class*='campus' i]"),
        ("Location", "Building", "Campus", "School"))
    details["title"] = field(
        (".fsTitles", ".fsRoles", ".fsConstituentTitle", "[class*='title' i]"),
        ("Title", "Position", "Role"))
    return details


def _campus_from_photo(card) -> Optional[str]:
    """Finalsite photo paths often encode the campus, e.g.
    /uploaded/faculty/AHS/adams.jpg -> 'AHS'. Extract that code when present."""
    img = card.find("img")
    if not img:
        return None
    src = img.get("src") or img.get("data-src") or ""
    m = re.search(r"/(?:faculty|staff|uploaded)/([A-Za-z0-9 _-]{1,20})/[^/]+$",
                  src)
    if not m:
        return None
    code = m.group(1).strip()
    low = code.lower()
    if low in ("faculty", "staff", "uploaded", "images", "photos", "thumbs",
               "default", "admin_photos"):
        return None
    return code


_CAMPUS_STOPWORDS = {
    "pre-k", "prek", "pre", "kindergarten", "teacher", "instructor", "aide",
    "at", "the", "of", "for", "in", "and", "&", "-", "assistant", "principal",
    "counselor", "coach", "director", "coordinator", "staff", "faculty",
    "grade", "math", "science", "english", "reading", "history", "art",
    "music", "pe", "special", "education", "ed", "esl", "bilingual",
    "elementary", "middle", "high", "school",  # trimmed only if leading
}


def _tidy_campus(name: str) -> str:
    """Drop leading role/connector words so we keep just the place name,
    e.g. 'Pre-K Teacher at Blanton Elementary' -> 'Blanton Elementary'."""
    words = name.split()
    # Trim from the left while the leading word is a stopword AND at least the
    # final two words (the place + type) remain.
    while len(words) > 2 and words[0].lower().strip(".,") in _CAMPUS_STOPWORDS:
        words.pop(0)
    return " ".join(words)


# School/campus keywords used to spot a real place name inside free text.
_SCHOOL_NAME_RE = re.compile(
    r"([A-Z][A-Za-z.'&-]+(?:\s+[A-Za-z.'&-]+){0,4}\s+"
    r"(?:Elementary|Intermediate|Middle|Junior High|High\s*School|"
    r"Primary|Preparatory|Prep|Academy|Early\s+(?:College|Childhood)"
    r"(?:\s+(?:High\s*School|Center))?|Learning\s+Center|Campus|School|"
    r"ISD|CISD))", re.I)

# Common campus-code -> level guesses, used only to make a bare code readable.
_CODE_LEVEL = [
    (re.compile(r"(?:^|_)(HS|H\.?S)$|HIGH", re.I), "High School"),
    (re.compile(r"(?:^|_)(MS|M\.?S|JH|JHS)$|MIDDLE|JUNIOR", re.I), "Middle School"),
    (re.compile(r"(?:^|_)(IS|INT)$|INTERMEDIATE", re.I), "Intermediate School"),
    (re.compile(r"(?:^|_)(ES|EL|ELEM)$|ELEMENTARY", re.I), "Elementary School"),
    (re.compile(r"(?:^|_)(ECC|ECE|PK|PRE)$|EARLY|PRE-?K", re.I),
     "Early Childhood"),
    (re.compile(r"ADMIN|CENTRAL|DISTRICT|DO\b", re.I), "Administration"),
]


def _readable_campus(code: str) -> str:
    """Turn a photo code like 'AHS' or 'aecc' into something readable when we
    can only guess the level; otherwise return the code as-is."""
    if not code:
        return ""
    for rx, level in _CODE_LEVEL:
        if rx.search(code):
            return level
    return code


def _campus_from_text(card) -> Optional[str]:
    """Look for a spelled-out school/campus name anywhere in the card text
    (title, department, location, bio) — e.g. 'Akins Early College High School',
    'Blanton Elementary'."""
    # Prefer explicit location/department/building fields if present.
    for sel in (".fsLocations", ".fsBuildings", ".fsDepartments",
                "[class*='location' i]", "[class*='building' i]",
                "[class*='campus' i]", "[class*='school' i]",
                ".fsTitles", ".fsRoles", ".title", ".position"):
        for el in card.select(sel):
            m = _SCHOOL_NAME_RE.search(el.get_text(" "))
            if m:
                name = _tidy_campus(clean_text(m.group(1)))
                if 4 < len(name) < 70:
                    return name
    # Fall back to the whole card text.
    m = _SCHOOL_NAME_RE.search(card.get_text(" "))
    if m:
        name = clean_text(m.group(1))
        if 4 < len(name) < 70:
            return name
    return None


def _detect_campus(card, default_school: str) -> str:
    """Best campus/school name for a person: a spelled-out place name if we can
    find one, else a readable version of the photo-path code, else the default,
    else blank. Never returns the district name (caller passes '' as default)."""
    return (_campus_from_text(card)
            or _readable_campus(_campus_from_photo(card) or "")
            or default_school or "")


def parse_finalsite_page(html: str, district: str, school: str,
                         directory_url: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(".fsConstituentItem, .fsConstituentProfile")
    if not cards:
        cards = soup.select("[class*='fsConstituent'], [class*='constituent' i]")
    out: List[Dict] = []
    for card in cards:
        name_el = card.select_one(
            ".fsFullName, .fsConstituentName, .fsFullNameLink, h3, h4, .name")
        if not name_el:
            continue
        name = clean_text(name_el.get_text())
        if not name or len(name) < 3:
            continue
        title_el = card.select_one(
            ".fsTitles, .fsConstituentTitle, .fsRoles, .title, .position")
        title = clean_text(title_el.get_text()) if title_el else ""
        title = re.sub(r"^\s*(roles?|titles?)\s*:\s*", "", title, flags=re.I)
        email = _extract_card_email(card)
        campus = _detect_campus(card, school)
        # Finalsite gives each person a stable id — best dedup / repeat key.
        cid = card.get("data-constituent-id") or ""
        # Profile link (used to fetch email when it's not on the list card).
        prof = card.select_one(
            "a.fsConstituentProfileLink, a.fsFullNameLink, .fsFullName a, "
            "a[href*='/profile'], a[href*='constituent']")
        prof_url = ""
        if prof and prof.get("href") and prof["href"] not in ("#", ""):
            prof_url = urljoin(directory_url, prof["href"])
        first, last = normalize_name(name)
        role, _d, etype = infer_role(name, title, email, directory_url,
                                     campus or district)
        final_role = title or role
        lead = classify_lead(final_role, role, etype, district, campus)
        out.append({"district": district, "school": campus,
                    "first_name": first, "last_name": last, "email": email,
                    "role": final_role, "department": "", "lead_type": lead,
                    "_cid": cid,
                    "_profile": prof_url,
                    "_key": cid or f"{name}|{email}"})
    return out


def _set_const_page(url: str, page: int) -> str:
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q["const_page"] = [str(page)]
    return urlunparse(parts._replace(
        query=urlencode({k: v[0] for k, v in q.items()})))


def _total_pages(html: str, cap: int = 40) -> int:
    m = re.search(r"showing\s+\d+\s*-\s*(\d+)\s+of\s+(\d+)", html, re.I)
    if m:
        per, total = int(m.group(1)), int(m.group(2))
        if per > 0:
            return min(cap, max(1, -(-total // per)))
    pages = [int(x) for x in re.findall(r"const_page=(\d+)", html)]
    return min(cap, max(pages) if pages else 1)


_NEXT_SELECTORS = [
    "a.fsNextPageLink", ".fsElementPagination a[rel='next']",
    "a[rel='next']", ".fsPagination a.fsNextPageLink",
    ".fsElementPagination .fsNextPageLink",
    "a[aria-label*='Next' i]", ".pagination a.next",
    "li.fsNextPageItem a", ".fsPaginationNext a",
]


def _compute_total_pages(html: str, cap: int = 1000) -> int:
    """From 'showing 1 - 12 of 4487 constituents' compute the page count."""
    m = re.search(r"showing\s+\d+\s*-\s*(\d+)\s+of\s+([\d,]+)", html, re.I)
    if m:
        per = int(m.group(1))
        total = int(m.group(2).replace(",", ""))
        if per > 0:
            return min(cap, max(1, -(-total // per)))  # ceil
    pages = [int(x) for x in re.findall(r"const_page=(\d+)", html)]
    return min(cap, max(pages) if pages else 1)


async def _scrape_one(context, url: str, district: str, wait_ms: int,
                      max_pages: int = 1000, follow_profiles: bool = True,
                      profile_wait_ms: int = 1200,
                      max_profiles: int = 600) -> List[Dict]:
    """Render the directory and collect every page. Strategy:
      1. Read the 'showing X of Y' total to know how many pages exist.
      2. Try direct ?const_page=N navigation. If page 2 yields NEW people,
         loop the URL directly through all pages (fast, gets big directories
         like 4,487 staff / 374 pages).
      3. If const_page is ignored (some Finalsite skins re-render page 1),
         fall back to CLICKING the next control.
    Dedup by constituent-id; fills missing emails via profile follow."""
    page = await context.new_page()
    collected: Dict[str, Dict] = {}

    async def harvest(cur_url: str) -> int:
        await page.wait_for_timeout(wait_ms)
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(300)
        html = await page.content()
        rows = parse_finalsite_page(html, district, "", cur_url)
        new = 0
        for r in rows:
            k = r["_key"]
            if k and k not in collected:
                collected[k] = r
                new += 1
        if follow_profiles and any(
                r["_key"] in collected and not collected[r["_key"]].get("email")
                for r in rows):
            try:
                await _fill_profiles_by_click(
                    page, collected, wait_ms=profile_wait_ms,
                    max_profiles=max_profiles, context=context)
            except Exception as exc:
                logger.debug("profile fill failed: %s", exc)
        return new

    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            logger.debug("goto failed %s: %s", url, exc)
            return []

        await harvest(url)
        total_pages = min(max_pages, _compute_total_pages(await page.content()))

        # --- try direct const_page navigation ---
        use_direct = False
        if total_pages > 1:
            p2 = _set_const_page(url, 2)
            try:
                await page.goto(p2, wait_until="domcontentloaded",
                                timeout=45000)
                if await harvest(p2) > 0:
                    use_direct = True
            except Exception as exc:
                logger.debug("const_page probe failed: %s", exc)

        if use_direct:
            for pg in range(3, total_pages + 1):
                pu = _set_const_page(url, pg)
                try:
                    await page.goto(pu, wait_until="domcontentloaded",
                                    timeout=45000)
                    if await harvest(pu) == 0:
                        break  # ran past the last populated page
                except Exception as exc:
                    logger.debug("page %d failed: %s", pg, exc)
                    break
        else:
            # --- click-based fallback (const_page ignored by this skin) ---
            for _ in range(total_pages if total_pages > 1 else max_pages):
                next_el = None
                for sel in _NEXT_SELECTORS:
                    try:
                        el = await page.query_selector(sel)
                    except Exception:
                        el = None
                    if not el:
                        continue
                    try:
                        disabled = await el.get_attribute("aria-disabled")
                        cls = (await el.get_attribute("class")) or ""
                        if disabled == "true" or "disabled" in cls.lower():
                            continue
                        if not await el.is_visible():
                            continue
                    except Exception:
                        pass
                    next_el = el
                    break
                if not next_el:
                    break
                try:
                    await next_el.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    await page.wait_for_timeout(1200)
                if await harvest(page.url) == 0:
                    break

        return list(collected.values())
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _fill_profiles_by_click(page, collected: Dict[str, Dict],
                                  wait_ms: int, max_profiles: int,
                                  context) -> int:
    """For each person on the current directory page whose email is missing,
    click their profile control and read the resulting profile — whether it
    opens as a modal (same page) or navigates to a new profile page. Updates
    the matching records in `collected`. Returns number of emails filled."""
    filled = 0
    # Re-locate profile links on the page (by constituent id where possible).
    handles = await page.query_selector_all(
        ".fsConstituentItem a.fsConstituentProfileLink, "
        ".fsConstituentItem a.fsFullNameLink, "
        ".fsConstituentItem .fsFullName a, "
        ".fsConstituentProfile a.fsConstituentProfileLink")
    directory_url = page.url

    for h in handles[:max_profiles]:
        try:
            cid = (await h.get_attribute("data-constituent-id")) or ""
            # find the matching record (by cid, else by visible name text)
            rec = None
            if cid and cid in collected:
                rec = collected[cid]
            if rec is None:
                nm = clean_text(await h.inner_text())
                for r in collected.values():
                    if nm and nm.lower() in (
                            f"{r['first_name']} {r['last_name']}").lower():
                        rec = r
                        break
            if rec is None or rec.get("email"):
                continue

            href = (await h.get_attribute("href")) or ""
            profile_html = None

            if href and href not in ("#", "") and "javascript" not in href.lower():
                # NEW-PAGE pattern: open the profile URL in a side page.
                pg = await context.new_page()
                try:
                    await pg.goto(urljoin(directory_url, href),
                                  wait_until="domcontentloaded", timeout=30000)
                    await pg.wait_for_timeout(wait_ms)
                    profile_html = await pg.content()
                finally:
                    await pg.close()
            else:
                # MODAL pattern: click and read the pop-up that appears.
                pre = await page.content()
                try:
                    await h.click()
                except Exception:
                    await h.evaluate("el => el.click()")
                await page.wait_for_timeout(wait_ms)
                modal = await page.query_selector(
                    ".fsModal, .fsModalContent, [role='dialog'], "
                    ".fsElementModal, .modal.show, .fsConstituentProfile")
                if modal:
                    profile_html = await modal.inner_html()
                else:
                    post = await page.content()
                    if post != pre:
                        profile_html = post
                # close the modal so the next click works
                for csel in (".fsModalClose", "[aria-label*='close' i]",
                             ".fsClose", "button.close"):
                    btn = await page.query_selector(csel)
                    if btn:
                        try:
                            await btn.click()
                            await page.wait_for_timeout(300)
                        except Exception:
                            pass
                        break
                else:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)

            if profile_html:
                det = _extract_profile_details(BeautifulSoup(profile_html, "lxml"))
                if det.get("email"):
                    rec["email"] = det["email"]
                    filled += 1
                if det.get("location") and not rec.get("school"):
                    rec["school"] = det["location"]
                if det.get("department"):
                    rec["department"] = det["department"]
                if det.get("title") and (not rec.get("role")
                                         or rec["role"] in ("Staff", "")):
                    rec["role"] = det["title"]
        except Exception as exc:
            logger.debug("profile click failed: %s", exc)
            continue
    return filled


# ---- orchestration ---------------------------------------------------------

_COMMON_FS_PATHS = [
    "/staff-directory", "/quick-links/staff-directory",
    "/our-district/staff-directory", "/directory",
    "/our-district/directory", "/about/staff-directory",
    "/district/staff-directory", "/staff/staff-directory",
    "/departments/staff-directory", "/staff-directories",
]


def _base_site(url: str) -> str:
    try:
        p = urlparse(url if url.startswith("http") else "https://" + url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return url.rstrip("/")


async def _probe_directory_url(context, seed_url: str, wait_ms: int) -> str:
    """The profiler often stored a landing page, not the constituent
    directory. Try the seed URL first, then common Finalsite directory paths
    on the same host; return the first that actually has staff cards."""
    candidates = []
    if seed_url:
        candidates.append(seed_url)
    base = _base_site(seed_url)
    for path in _COMMON_FS_PATHS:
        cand = base + path
        if cand not in candidates:
            candidates.append(cand)
    for cand in candidates:
        page = await context.new_page()
        try:
            await page.goto(cand, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(min(wait_ms, 1500))
            soup = BeautifulSoup(await page.content(), "lxml")
            if soup.select(".fsConstituentItem, .fsConstituentProfile, "
                           "[class*='fsConstituent']"):
                return cand
        except Exception:
            pass
        finally:
            await page.close()
    return seed_url


async def _run_async(recs: List[Dict], output_csv: str, concurrency: int,
                     wait_ms: int, headless: bool, checkpoint_every: int,
                     follow_profiles: bool = True,
                     probe_paths: bool = False) -> List[Dict]:
    from playwright.async_api import async_playwright

    all_rows: List[Dict] = []
    out_path = Path(output_csv)

    def flush():
        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS,
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(all_rows)

    try:
        from tqdm.auto import tqdm
        pbar = tqdm(total=len(recs), desc="Finalsite", unit="dist")
    except Exception:
        pbar = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        sem = asyncio.Semaphore(max(1, concurrency))
        done = {"n": 0}

        async def worker(rec):
            district = rec.get("district_name", "")
            durl = rec.get("staff_directory_url", "")
            async with sem:
                context = await browser.new_context(
                    user_agent=_UA, ignore_https_errors=True)
                try:
                    if probe_paths:
                        durl = await _probe_directory_url(context, durl, wait_ms)
                    rows = await _scrape_one(context, durl, district, wait_ms,
                                             follow_profiles=follow_profiles)
                except Exception as exc:
                    logger.warning("  %s failed: %s", district[:30], exc)
                    rows = []
                finally:
                    await context.close()
            all_rows.extend(rows)
            em = sum(1 for r in rows if r["email"])
            logger.info("  %-38s %4d staff | %4d emails",
                        district[:38], len(rows), em)
            done["n"] += 1
            if pbar:
                pbar.update(1)
            if done["n"] % checkpoint_every == 0:
                flush()
            return rows

        await asyncio.gather(*(worker(r) for r in recs))
        await browser.close()

    if pbar:
        pbar.close()
    flush()
    return all_rows


def run_finalsite(targets_csv: str,
                  output_csv: str = "finalsite_leads.csv",
                  concurrency: int = 3,
                  only_cms_finalsite: bool = True,
                  limit: int = 0,
                  districts: Optional[List[str]] = None,
                  only_districts_in: Optional[List[str]] = None,
                  wait_ms: int = 2500,
                  headless: bool = True,
                  follow_profiles: bool = True,
                  probe_paths: bool = False,
                  checkpoint_every: int = 5):
    """Entry point. Requires: nest_asyncio.apply() called once in the notebook.

    concurrency : parallel browser contexts (3-5; browsers are heavy on RAM).
    districts   : optional list of district-name substrings to target
                  (case-insensitive), e.g. ["ALVARADO", "CYPRESS-FAIRBANKS"].
                  When given, this OVERRIDES limit/cms filtering so you can
                  test exactly the districts you want.
    limit       : take the first N *Finalsite* districts (only when `districts`
                  is not given). Note: these are alphabetical, so some may be
                  Contact-Form / low-yield — prefer `districts=` for testing.
    """
    import pandas as pd
    setup_logging("INFO")

    src = pd.read_csv(targets_csv)
    src = src[src["staff_directory_url"].notna()].copy()

    if districts:
        # target specific named districts, regardless of cms/limit
        wanted = [d.strip().lower() for d in districts]
        mask = src["district_name"].astype(str).str.lower().apply(
            lambda n: any(w in n for w in wanted))
        src = src[mask]
    else:
        if only_cms_finalsite and "cms" in src.columns:
            src = src[src["cms"].astype(str).str.strip().str.lower()
                      == "finalsite"]

    # Restrict to an explicit set of district names (exact, case-insensitive).
    # Handy for re-running only the districts that already produced emails.
    if only_districts_in:
        keep = {d.strip().lower() for d in only_districts_in}
        src = src[src["district_name"].astype(str).str.strip().str.lower()
                  .isin(keep)]

    src = src.drop_duplicates("district_name").reset_index(drop=True)
    if districts is None and not only_districts_in and limit and limit > 0:
        src = src.head(limit)

    if src.empty:
        raise SystemExit(
            "No matching districts. Check the district name spelling, or that "
            "the cms column contains 'finalsite'.")
    logger.info("Finalsite (async) districts: %d (concurrency=%d)",
                len(src), concurrency)
    for nm in src["district_name"].tolist()[:15]:
        logger.info("   -> %s", nm)

    recs = [r._asdict() if hasattr(r, "_asdict") else dict(r)
            for r in src.itertuples(index=False)]

    try:
        loop = asyncio.get_event_loop()
        all_rows = loop.run_until_complete(
            _run_async(recs, output_csv, concurrency, wait_ms, headless,
                       checkpoint_every, follow_profiles=follow_profiles,
                       probe_paths=probe_paths))
    except RuntimeError as exc:
        raise SystemExit(
            "Async loop error — did you run `import nest_asyncio; "
            "nest_asyncio.apply()` first?\n" + str(exc))

    # de-dupe (scope email key to district so identical local-parts across
    # districts never collide; keep no-email people via name key)
    seen: Set[str] = set()
    unique: List[Dict] = []
    for r in all_rows:
        email = (r.get("email") or "").lower()
        key = f"{r['district']}|{email}" if email else \
            f"{r['district']}|{r['first_name']}|{r['last_name']}|{r['role']}"
        if key not in seen:
            seen.add(key)
            unique.append(r)

    with open(output_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(unique)

    n_email = sum(1 for r in unique if r["email"])
    print("\n" + "=" * 66)
    print(f"FINALSITE DONE. {len(unique)} leads across "
          f"{len({r['district'] for r in unique})} districts.")
    print(f"With email: {n_email} "
          f"({(n_email/len(unique)*100 if unique else 0):.0f}%)")
    print(f"Saved: {Path(output_csv).resolve()}")
    print("=" * 66)
    try:
        import pandas as pd
        return pd.read_csv(output_csv)
    except Exception:
        return output_csv


def debug_one(url: str, wait_ms: int = 3500, headless: bool = True):
    """Render ONE directory page and report what's actually in the DOM:
    email count, first-card HTML, selector hits. For diagnosing a district."""
    setup_logging("INFO")

    async def _go():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(
                user_agent=_UA, ignore_https_errors=True)
            html = await _render(context, url, wait_ms)
            await browser.close()
            return html

    loop = asyncio.get_event_loop()
    html = loop.run_until_complete(_go())
    print("rendered HTML length:", len(html) if html else None)
    if not html:
        print("STILL None — render failed even with async + ignore SSL.")
        return
    emails = _EMAIL_TEXT_RE.findall(html)
    print("emails in rendered DOM:", len(emails), "| sample:", emails[:5])
    soup = BeautifulSoup(html, "lxml")
    card = soup.select_one(".fsConstituentItem, .fsConstituentProfile")
    print("\n----- FIRST CARD HTML -----")
    print(card.prettify()[:2500] if card else "NO constituent card found")
    for sel in [".fsStyleSROnly", ".fsEmail", "a[href^='mailto']",
                "[class*='mail']", ".fsConstituentContact"]:
        els = soup.select(sel)
        print(f"selector {sel!r}: {len(els)} matches"
              + (f" | sample: {els[0].get_text(' ', strip=True)[:80]!r}"
                 if els else ""))
    return html


def _in_notebook() -> bool:
    try:
        from IPython import get_ipython
        return get_ipython().__class__.__name__ == "ZMQInteractiveShell"
    except Exception:
        return False


if __name__ == "__main__" and not _in_notebook():
    import sys
    run_finalsite(sys.argv[1] if len(sys.argv) > 1 else "all_tx_profiles.csv",
                  limit=3)
