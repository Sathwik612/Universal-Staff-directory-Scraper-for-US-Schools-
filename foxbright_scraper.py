#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Foxbright staff-directory scraper (a Michigan school CMS, footer "Powered by
Foxbright", CDN fxbrt.com). Directory list pages show name/building/position/
phone but NOT email; each staff member links to a detail page that DOES carry
the email. This scraper:

  1. loads the district-wide staff directory (static HTML, requests),
  2. paginates (Foxbright uses ?page=N or First/Prev/Next/Last links),
  3. for each staff row, follows the detail link and extracts the email
     (mailto:, bare text, or Cloudflare data-cfemail),
  4. returns district | school | first_name | last_name | email | role.

Runs in Colab. Uses requests only (no browser needed) — Foxbright is static.

USAGE:
    from foxbright_scraper import run_foxbright
    df = run_foxbright("mi_foxbright.csv", output_csv="mi_foxbright_leads.csv",
                       workers=12)
"""
from __future__ import annotations
import re
import csv
import time
import logging
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

logger = logging.getLogger("foxbright")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
OUTPUT_FIELDS = ["district", "school", "first_name", "last_name",
                 "email", "role", "lead_type"]


def _get(session, url, timeout=20):
    try:
        r = session.get(url, timeout=timeout, verify=False,
                        headers={"User-Agent": _UA})
        if r.status_code == 200:
            return r.text
    except Exception as exc:
        logger.debug("get failed %s: %s", url, exc)
    return None


def _decode_cfemail(hexstr):
    try:
        r = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i+2], 16) ^ r)
                       for i in range(2, len(hexstr), 2))
    except Exception:
        return None


def _email_from_soup(soup):
    # mailto
    for a in soup.select("a[href^='mailto:']") or []:
        e = a.get("href", "")[7:].split("?")[0]
        if EMAIL_RE.fullmatch(e or ""):
            return e.lower()
    a = soup.find("a", href=re.compile(r"^mailto:", re.I))
    if a:
        e = a["href"][7:].split("?")[0]
        if EMAIL_RE.fullmatch(e or ""):
            return e.lower()
    # cloudflare
    cf = soup.select_one("[data-cfemail]")
    if cf:
        dec = _decode_cfemail(cf.get("data-cfemail", ""))
        if dec and EMAIL_RE.fullmatch(dec):
            return dec.lower()
    # plain text
    m = EMAIL_RE.search(soup.get_text(" "))
    if m:
        return m.group(0).lower()
    return None


def _split_name(full):
    full = re.sub(r"\s+", " ", full or "").strip()
    # Foxbright shows "First Last"
    parts = full.split()
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return full, ""


def _parse_staff_entries(soup, base_url):
    """Parse Foxbright's real structure:
       <div class="fbcms_staffentry"><div class="fbcms_staffinfo">
         <div class="name">..</div><div class="row2 building">..</div>
         <div class="row3 position">..</div>
         [optional email as mailto link or text]
       </div></div>
    Returns list of dicts with name/building/position/email/detail_url."""
    out = []
    entries = soup.select(".fbcms_staffentry")
    for e in entries:
        name_el = e.select_one(".name")
        if not name_el:
            continue
        name = re.sub(r"\s+", " ", name_el.get_text(" ")).strip()
        if not name or len(name) < 3:
            continue
        building = e.select_one(".building")
        position = e.select_one(".position")
        building = re.sub(r"\s+", " ", building.get_text(" ")).strip() if building else ""
        position = re.sub(r"\s+", " ", position.get_text(" ")).strip() if position else ""
        # email may be present as mailto or text within the entry
        email = _email_from_soup(e)
        # or a detail link (some Foxbright skins link the name)
        detail = ""
        a = name_el.find("a", href=True) or e.find("a", href=re.compile(
            r"(staff|profile|detail|/user)", re.I))
        if a and a.get("href"):
            detail = urljoin(base_url, a["href"])
        out.append({"name": name, "building": building, "position": position,
                    "email": email, "url": detail})
    return out


_NEXT_RE = re.compile(r"(next|last)\s*page", re.I)


def _all_list_pages(session, start_url, max_pages=40):
    """Return combined soup list across pagination (Foxbright ?page=N)."""
    pages = []
    html = _get(session, start_url)
    if not html:
        return pages
    pages.append((start_url, html))
    # detect ?page= style
    soup = BeautifulSoup(html, "lxml")
    page_nums = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            page_nums.add(int(m.group(1)))
    for n in sorted(page_nums):
        if n <= 1 or n > max_pages:
            continue
        sep = "&" if "?" in start_url else "?"
        purl = f"{start_url}{sep}page={n}"
        h = _get(session, purl)
        if h:
            pages.append((purl, h))
    return pages


def scrape_foxbright_district(district, start_url, follow_profiles=True,
                              max_pages=40):
    session = requests.Session()
    out = []
    seen = set()
    for purl, html in _all_list_pages(session, start_url, max_pages):
        soup = BeautifulSoup(html, "lxml")
        entries = _parse_staff_entries(soup, purl)
        for s in entries:
            email = s["email"]
            # follow a detail page only if the list had no email but a link
            if not email and follow_profiles and s["url"]:
                dh = _get(session, s["url"])
                if dh:
                    email = _email_from_soup(BeautifulSoup(dh, "lxml"))
            key = email or f"{s['name']}|{s['building']}|{s['position']}"
            if key in seen:
                continue
            seen.add(key)
            fn, ln = _split_name(s["name"])
            role = s["position"] or ""
            lt = "Teacher" if "teacher" in role.lower() else (
                 "School" if s["building"] and "administ" not in s["building"].lower()
                 else "Dist")
            out.append({"district": district, "school": s["building"],
                        "first_name": fn, "last_name": ln,
                        "email": email or "", "role": role, "lead_type": lt})
    return out


def run_foxbright(targets_csv, output_csv="foxbright_leads.csv", workers=8,
                  follow_profiles=True, limit=0):
    import pandas as pd
    src = pd.read_csv(targets_csv)
    if limit:
        src = src.head(limit)
    tasks = list(src[["district_name", "staff_directory_url"]].itertuples(
        index=False, name=None))

    all_rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scrape_foxbright_district, d, u): d
                for d, u in tasks}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                rows = fut.result()
            except Exception as exc:
                logger.info("  %-40s ERROR %s", d, exc)
                rows = []
            done += 1
            n_email = sum(1 for r in rows if r["email"])
            logger.info("  %-40s %4d staff | %4d emails  (%d/%d)",
                        d[:40], len(rows), n_email, done, len(tasks))
            all_rows.extend(rows)
            # checkpoint
            with open(output_csv, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS,
                                   extrasaction="ignore")
                w.writeheader()
                w.writerows(all_rows)

    n_email = sum(1 for r in all_rows if r["email"])
    print("\n" + "=" * 60)
    print(f"FOXBRIGHT DONE. {len(all_rows)} staff across "
          f"{len({r['district'] for r in all_rows})} districts.")
    print(f"With email: {n_email} "
          f"({(n_email/len(all_rows)*100 if all_rows else 0):.0f}%)")
    print(f"Saved: {output_csv}")
    print("=" * 60)
    import pandas as pd
    return pd.DataFrame(all_rows)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        rows = scrape_foxbright_district("TEST", sys.argv[1])
        for r in rows[:20]:
            print(r)
        print("total:", len(rows),
              "emails:", sum(1 for r in rows if r["email"]))
