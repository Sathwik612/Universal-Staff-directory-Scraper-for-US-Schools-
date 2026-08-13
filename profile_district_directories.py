#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Profile & group Texas district staff directories by TYPE, so similar ones can
be scraped with the same extractor (batch by structure instead of one-by-one).

Idea: Austin ISD was easy because its directory is a clean HTML table with
mailto emails on a uniform template. Many other districts share a directory
"style" (email table / card grid / mailto list / JS-rendered / none) and/or a
CMS vendor (Finalsite, Apptegy, Edlio, Blackboard, Gabbart, Drupal, WordPress).
Group by those, and one extractor covers a whole bucket.

Input : the directory-links CSV from build_texas_staff_directory_links.run()
        (columns: district_name, website, staff_directory_url, status, ...)
Output: a profile CSV with a `dir_style` + `cms` per district, and printed
        bucket counts so you know which groups are worth writing an extractor
        for first.

Usage (Colab):
    from profile_district_directories import profile_directories
    prof = profile_directories("texas_district_staff_directories.csv",
                               output_csv="directory_profiles.csv", workers=12)
    # then, e.g., all the easy "email_table" districts:
    prof[prof.dir_style == "email_table"]
"""

from __future__ import annotations

import csv
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

from bs4 import BeautifulSoup

from master_staff_directory_scraper import (
    Fetcher, ScraperConfig, detect_cms, clean_text,
    setup_logging, logger,
)

OUTPUT_FIELDS = ["district_name", "website", "staff_directory_url", "cms",
                 "generator", "vendor_host", "dir_style", "austin_like",
                 "n_mailto", "n_table_rows", "n_cards", "notes"]

# Host substrings that reveal a common hosting vendor / template.
_VENDOR_HOST_HINTS = {
    "finalsite": ("finalsite",),
    "apptegy": ("apptegy", "thrillshare"),
    "edlio": ("edlio", "edl.io"),
    "blackboard": ("blackboard", "schoolwires", "myschoolcdn"),
    "gabbart": ("gabbart",),
    "esc_regional": (".esc", "escregion", "region"),
    "austinschools": ("austinschools.org",),
    "campusstar": ("campusstar",),
}


def _generator(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": re.compile("generator", re.I)})
    return clean_text(meta.get("content")) if meta and meta.get("content") else ""


def _vendor_host(html: str, url: str) -> str:
    hay = (html or "").lower() + " " + (url or "").lower()
    for vendor, hints in _VENDOR_HOST_HINTS.items():
        if any(h in hay for h in hints):
            return vendor
    return ""


def classify_directory(html: str, url: str) -> Dict:
    """Return a structural fingerprint of a staff-directory page."""
    soup = BeautifulSoup(html, "lxml")

    mailto = soup.find_all("a", href=re.compile(r"^mailto:", re.I))
    n_mailto = len(mailto)

    # Biggest table + whether it contains mailto links (email table pattern)
    best_rows = 0
    table_has_mail = False
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) > best_rows:
            best_rows = len(rows)
            table_has_mail = bool(
                table.find("a", href=re.compile(r"^mailto:", re.I)))

    cards = soup.select(
        '[class*="staff"], [class*="employee"], [class*="faculty"], '
        '[class*="person"], [class*="directory-item"]')
    n_cards = len(cards)

    text_len = len(clean_text(soup.get_text(" ")))
    generator = _generator(soup)
    cms = detect_cms(html, url)
    vendor = _vendor_host(html, url)

    # Decide the style (priority order).
    if best_rows >= 4 and (table_has_mail or n_mailto >= 3):
        style = "email_table"
    elif n_cards >= 3 and n_mailto >= 3:
        style = "card_grid_email"
    elif n_cards >= 3 and n_mailto == 0:
        style = "card_grid_no_email"   # names present, emails on profiles
    elif n_mailto >= 5:
        style = "mailto_list"
    elif text_len < 400 or (n_cards == 0 and best_rows < 2 and n_mailto == 0):
        style = "js_or_empty"          # likely JS-rendered or a stub page
    else:
        style = "unknown"

    austin_like = bool(
        style == "email_table" and ("drupal" in generator.lower()
                                    or vendor in ("austinschools", "esc_regional")
                                    or url.rstrip("/").endswith("/directory")))

    return {
        "cms": cms.value, "generator": generator, "vendor_host": vendor,
        "dir_style": style, "austin_like": austin_like, "n_mailto": n_mailto,
        "n_table_rows": best_rows, "n_cards": n_cards,
    }


def profile_directories(directory_csv: str,
                        output_csv: str = "directory_profiles.csv",
                        workers: int = 12,
                        verify_ssl: bool = False,
                        max_districts: int = 0,
                        checkpoint_every: int = 20):
    """Fingerprint each district's staff directory and group by type."""
    import pandas as pd
    setup_logging("INFO")

    src = pd.read_csv(directory_csv)
    # Only rows that actually have a directory URL to look at.
    if "staff_directory_url" in src.columns:
        src = src[src["staff_directory_url"].notna()]
    src = src.drop_duplicates("district_name").reset_index(drop=True)
    if max_districts and max_districts > 0:
        src = src.head(max_districts)
    logger.info("Profiling %d district directories (workers=%d)",
                len(src), workers)

    cfg = ScraperConfig(respect_robots=True, rate_limit_delay=0.2,
                        verify_ssl=verify_ssl, request_timeout=15)
    _tls = threading.local()

    def get_fetcher():
        f = getattr(_tls, "f", None)
        if f is None:
            f = _tls.f = Fetcher(cfg)
        return f

    rows_out: List[Dict] = []
    lock = threading.Lock()
    out_path = Path(output_csv)

    def flush():
        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
            w.writeheader()
            w.writerows(rows_out)

    def work(rec: Dict) -> Dict:
        url = rec.get("staff_directory_url") or rec.get("website")
        out = {
            "district_name": rec.get("district_name", ""),
            "website": rec.get("website", ""),
            "staff_directory_url": rec.get("staff_directory_url", ""),
            "cms": "", "generator": "", "vendor_host": "",
            "dir_style": "unreachable", "austin_like": False,
            "n_mailto": 0, "n_table_rows": 0, "n_cards": 0, "notes": "",
        }
        html = get_fetcher().get(url) if url else None
        if not html:
            return out
        try:
            out.update(classify_directory(html, url))
        except Exception as exc:
            out["notes"] = f"parse_error: {exc}"[:120]
        return out

    recs = [r._asdict() if hasattr(r, "_asdict") else dict(r)
            for r in src.itertuples(index=False)]

    try:
        from tqdm import tqdm
        pbar = tqdm(total=len(recs), desc="Profiling", unit="dist")
    except Exception:
        pbar = None

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, r) for r in recs]
        for fut in as_completed(futures):
            try:
                with lock:
                    rows_out.append(fut.result())
            except Exception as exc:
                logger.debug("profile task failed: %s", exc)
            done += 1
            if pbar:
                pbar.update(1)
            if done % checkpoint_every == 0:
                with lock:
                    flush()
    if pbar:
        pbar.close()
    flush()

    # ---- summary ----
    import pandas as pd
    df = pd.DataFrame(rows_out, columns=OUTPUT_FIELDS)
    print("\n" + "=" * 66)
    print("DIRECTORY STYLE BUCKETS  (how to scrape each group):")
    print("=" * 66)
    style_help = {
        "email_table": "table w/ emails  -> reuse Austin-style table extractor",
        "card_grid_email": "cards w/ emails -> card parser (already supported)",
        "card_grid_no_email": "cards, emails on profiles -> follow profiles",
        "mailto_list": "loose mailto list -> mailto parser",
        "js_or_empty": "JavaScript-rendered/stub -> needs JS or per-site work",
        "unknown": "unrecognized -> inspect manually",
        "unreachable": "site didn't load (dead/blocked/bad cert)",
    }
    for style, cnt in df["dir_style"].value_counts().items():
        print(f"  {style:20} {cnt:4}   {style_help.get(style,'')}")
    print("-" * 66)
    print(f"  Austin-like (uniform email table): "
          f"{int(df['austin_like'].sum())}")
    print("\nCMS vendors:")
    for cms, cnt in df["cms"].value_counts().head(10).items():
        print(f"  {cms:20} {cnt:4}")
    print("=" * 66)
    print(f"Saved: {out_path.resolve()}")
    return df


def _in_notebook() -> bool:
    try:
        from IPython import get_ipython
        return get_ipython().__class__.__name__ == "ZMQInteractiveShell"
    except Exception:
        return False


if __name__ == "__main__" and not _in_notebook():
    import sys
    csv_in = sys.argv[1] if len(sys.argv) > 1 else \
        "texas_district_staff_directories.csv"
    profile_directories(csv_in)
