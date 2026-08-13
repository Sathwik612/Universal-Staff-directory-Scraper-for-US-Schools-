#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
BUILD TEXAS STAFF-DIRECTORY LINKS  (companion to master_staff_directory_scraper)
================================================================================
Goal: for ~30% of Texas public school districts, find each district's staff /
faculty directory URL and write them to a file.

Why this is a two-step pipeline (please read):
  * The authoritative list of Texas districts + their websites is TEA's AskTED
    "Download School and District File" (a daily CSV). You download it once
    (one click) from:
        https://tealprod.tea.state.tx.us/Tea.AskTed.Web/Forms/DownloadDefault.aspx
  * Finding each district's *staff directory* URL requires visiting the live
    district site. This script does that using the scraper's directory finder.
    It cannot be faked - a made-up URL is worse than none - so it runs live.

This is designed for Google Colab but works anywhere.

--------------------------------------------------------------------------------
QUICK START (Colab)
--------------------------------------------------------------------------------
    # 1) install + get the scraper module in the same folder, then:
    !pip install -q requests beautifulsoup4 lxml pandas openpyxl tqdm

    # 2) download the AskTED CSV (one click) from the URL above and upload it,
    #    OR let the experimental auto-download try:
    from build_texas_staff_directory_links import run
    run(
        askted_csv="AskTED_download.csv",   # path you uploaded; or None to auto-try
        sample_fraction=0.30,
        output_csv="texas_district_staff_directories.csv",
        use_javascript=False,               # True needs Playwright installed
    )

Output columns:
    district_number, district_name, county, region, website,
    staff_directory_url, method, status
================================================================================
"""

from __future__ import annotations

import csv
import io
import random
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# Reuse everything from the main scraper module. It must be importable
# (same folder in Colab, or installed).
from master_staff_directory_scraper import (
    Fetcher, ScraperConfig, School, is_directory_page, find_directories,
    get_domain, setup_logging, logger,
)

ASKTED_DOWNLOAD_URL = (
    "https://tealprod.tea.state.tx.us/Tea.AskTed.Web/Forms/DownloadDefault.aspx"
)

# Common staff-directory paths tried directly before crawling (fast path).
COMMON_DIRECTORY_PATHS = [
    "/staff-directory", "/staff_directory", "/staffdirectory",
    "/directory", "/staff", "/our-staff", "/ourstaff",
    "/faculty-staff", "/faculty-and-staff", "/faculty", "/personnel",
    "/staff-directory.aspx", "/directory.aspx",
    "/departments/human-resources/staff-directory",
    "/about/staff-directory", "/about-us/staff-directory",
    "/staff/staff-directory",
]


# ============================================================================
# STEP 1: LOAD THE ASKTED DISTRICT LIST
# ============================================================================

def _read_csv_bytes(data: bytes) -> List[Dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    # AskTED prepends apostrophes to zero-padded numbers; strip them.
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip().lstrip("'")
                     for k, v in row.items()})
    return rows


def _find_column(headers: List[str], *needles: str) -> Optional[str]:
    for h in headers:
        hl = h.lower()
        if all(n in hl for n in needles):
            return h
    return None


def _looks_like_url(v: str) -> bool:
    v = (v or "").strip().lower()
    return v.startswith("http") or v.startswith("www.") or (
        "." in v and " " not in v and len(v) > 4 and "@" not in v
        and any(v.endswith(t) or (t + "/") in v
                for t in (".org", ".net", ".com", ".edu", ".us")))


def _normalize_url(v: str) -> Optional[str]:
    v = (v or "").strip()
    if not v or "@" in v:
        return None
    if not v.lower().startswith("http"):
        v = "https://" + v.lstrip("/")
    # basic sanity
    host = get_domain(v)
    if not host or "." not in host:
        return None
    return v


def load_askted_districts(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """From a mixed school+district AskTED CSV, return one row per district
    that has a website. Uses tolerant column detection so it survives header
    changes across AskTED export years."""
    if not rows:
        return []
    headers = list(rows[0].keys())

    col_dnum = _find_column(headers, "district", "number") \
        or _find_column(headers, "district", "no") \
        or _find_column(headers, "district", "id")
    col_dname = _find_column(headers, "district", "name")
    col_cname = (_find_column(headers, "school", "name")
                 or _find_column(headers, "campus", "name"))
    col_cnum = (_find_column(headers, "school", "number")
                or _find_column(headers, "campus", "number"))
    col_county = _find_column(headers, "county")
    col_region = _find_column(headers, "region")

    # Website column: prefer a header mentioning web/url; else sniff values.
    col_web = (_find_column(headers, "web") or _find_column(headers, "url")
               or _find_column(headers, "http"))
    if not col_web:
        # sniff: pick the column whose values most often look like URLs
        best, best_score = None, 0
        for h in headers:
            score = sum(1 for r in rows[:500] if _looks_like_url(r.get(h, "")))
            if score > best_score:
                best, best_score = h, score
        if best_score >= 5:
            col_web = best

    logger.info("AskTED columns -> district_no=%r name=%r website=%r "
                "county=%r region=%r", col_dnum, col_dname, col_web,
                col_county, col_region)
    if not col_dname:
        raise RuntimeError(
            "Could not find a 'District Name' column in the AskTED file. "
            f"Headers seen: {headers}")

    # Identify district-level rows: campus number is blank/000, OR the
    # campus/school name is blank/equal to district name.
    def is_district_row(r: Dict[str, str]) -> bool:
        if col_cnum:
            cn = re.sub(r"\D", "", r.get(col_cnum, ""))
            if cn in ("", "0", "000", "0000"):
                return True
        if col_cname:
            cname = r.get(col_cname, "").strip().lower()
            dname = r.get(col_dname, "").strip().lower()
            if not cname or cname == dname:
                return True
        # If there's no campus column at all, every row is a district.
        return not (col_cnum or col_cname)

    districts: Dict[str, Dict[str, str]] = {}
    for r in rows:
        dname = r.get(col_dname, "").strip()
        if not dname:
            continue
        key = (r.get(col_dnum, "").strip() or dname).lower()
        website = _normalize_url(r.get(col_web, "")) if col_web else None
        rec = districts.get(key)
        # Keep the district-level row, and make sure we capture a website
        # from whichever row on this district happens to carry it.
        if rec is None:
            districts[key] = {
                "district_number": r.get(col_dnum, "") if col_dnum else "",
                "district_name": dname,
                "county": r.get(col_county, "") if col_county else "",
                "region": r.get(col_region, "") if col_region else "",
                "website": website or "",
                "_is_district_row": is_district_row(r),
            }
        else:
            if not rec["website"] and website:
                rec["website"] = website
            if is_district_row(r):
                rec["_is_district_row"] = True

    out = [v for v in districts.values()]
    for v in out:
        v.pop("_is_district_row", None)
    # Keep only districts that have a usable website.
    with_web = [d for d in out if d["website"]]
    logger.info("Districts parsed: %d total, %d with a website",
                len(out), len(with_web))
    return sorted(with_web, key=lambda d: d["district_name"])


def try_auto_download_askted() -> Optional[bytes]:
    """Best-effort automatic download of the AskTED CSV (ASP.NET postback).
    Returns CSV bytes or None. Manual download is more reliable; this is a
    convenience that may break if TEA changes the form."""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        r = s.get(ASKTED_DOWNLOAD_URL, timeout=60)
        soup = BeautifulSoup(r.text, "lxml")

        def field(fid):
            el = soup.find("input", id=fid)
            return el.get("value", "") if el else ""

        data = {
            "__VIEWSTATE": field("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": field("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": field("__EVENTVALIDATION"),
        }
        # include any submit button(s)
        for btn in soup.find_all("input", {"type": ["submit", "image"]}):
            name = btn.get("name")
            if name:
                data[name] = btn.get("value", "Download")
        # include selects at their default value
        for sel in soup.find_all("select"):
            name = sel.get("name")
            opt = sel.find("option", selected=True) or sel.find("option")
            if name and opt:
                data[name] = opt.get("value", "")
        r2 = s.post(ASKTED_DOWNLOAD_URL, data=data, timeout=180)
        ctype = r2.headers.get("Content-Type", "").lower()
        looks_csv = ("csv" in ctype or "octet" in ctype
                     or "," in r2.text[:500] and "district" in r2.text[:2000].lower())
        if looks_csv and len(r2.content) > 1000:
            return r2.content
    except Exception as exc:
        logger.warning("AskTED auto-download failed: %s", exc)
    return None


# ============================================================================
# STEP 2: RESOLVE EACH DISTRICT'S STAFF-DIRECTORY URL (live)
# ============================================================================

def resolve_staff_directory(fetcher: Fetcher, website: str, district_name: str,
                            config: ScraperConfig) -> Tuple[Optional[str], str]:
    """Return (staff_directory_url, method). method in {path, crawl, none}."""
    website = _normalize_url(website) or website

    # Fast path: probe common directory paths directly.
    for path in COMMON_DIRECTORY_PATHS:
        candidate = website.rstrip("/") + path
        html = fetcher.get(candidate)
        if not html:
            continue
        try:
            if is_directory_page(BeautifulSoup(html, "lxml")):
                return candidate, "path"
        except Exception:
            continue

    # Fallback: crawl homepage links using the scraper's directory finder.
    school = School(name=district_name, url=website, district=district_name)
    try:
        dirs = find_directories(fetcher, school, config)
    except Exception as exc:
        logger.debug("crawl failed for %s: %s", website, exc)
        dirs = []
    if dirs:
        # Prefer URLs that clearly look like a staff directory.
        def score(u: str) -> int:
            ul = u.lower()
            return (("staff" in ul) * 2 + ("directory" in ul) * 2
                    + ("faculty" in ul))
        dirs.sort(key=score, reverse=True)
        return dirs[0], "crawl"

    return None, "none"


# ============================================================================
# STEP 3: ORCHESTRATION
# ============================================================================

OUTPUT_FIELDS = ["district_number", "district_name", "county", "region",
                 "website", "staff_directory_url", "method", "status"]


def run(askted_csv: Optional[str] = None,
        sample_fraction: float = 0.30,
        output_csv: str = "texas_district_staff_directories.csv",
        use_javascript: bool = False,
        random_seed: int = 42,
        limit: int = 0,
        rate_limit_delay: float = 0.4,
        checkpoint_every: int = 20,
        workers: int = 12) -> str:
    """End-to-end. Returns the output CSV path.

    askted_csv : path to a downloaded AskTED "School and District File" CSV.
                 If None, an automatic download is attempted (less reliable).
    sample_fraction : fraction of districts to process (0.30 = 30%).
    limit : hard cap on districts processed (0 = no cap); handy for a test run.
    workers : districts resolved in parallel (this is network-bound, so more
              workers = much faster). 12 is a good default.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    setup_logging("INFO")

    # ---- load AskTED ----
    if askted_csv and Path(askted_csv).exists():
        data = Path(askted_csv).read_bytes()
        logger.info("Loaded AskTED file: %s (%d bytes)", askted_csv, len(data))
    else:
        logger.info("No AskTED file given; attempting auto-download ...")
        data = try_auto_download_askted()
        if not data:
            raise SystemExit(
                "\nCould not obtain the AskTED district file automatically.\n"
                "Please download it (one click) from:\n  "
                + ASKTED_DOWNLOAD_URL +
                "\nthen re-run with askted_csv='path/to/that/file.csv'.")

    rows = _read_csv_bytes(data)
    districts = load_askted_districts(rows)
    if not districts:
        raise SystemExit("No districts with websites found in the AskTED file.")

    # ---- sample 30% ----
    n = max(1, round(len(districts) * sample_fraction))
    sample = random.Random(random_seed).sample(districts, n)
    sample.sort(key=lambda d: d["district_name"])
    if limit and limit > 0:
        sample = sample[:limit]
    logger.info("Selected %d of %d districts (%.0f%%)%s | workers=%d",
                len(sample), len(districts), sample_fraction * 100,
                f" [capped at {limit}]" if limit else "", workers)

    # ---- resolve staff directories (live, in parallel) ----
    config = ScraperConfig(use_javascript=use_javascript,
                           respect_robots=True,
                           rate_limit_delay=rate_limit_delay,
                           directory_crawl_depth=1,
                           max_directories_per_school=6)

    out_path = Path(output_csv)
    results: List[Dict[str, str]] = []
    lock = threading.Lock()
    _tls = threading.local()

    def get_fetcher():
        f = getattr(_tls, "f", None)
        if f is None:
            f = _tls.f = Fetcher(config)
        return f

    def flush():
        # keep output stable/sorted by district name
        ordered = sorted(results, key=lambda r: r["district_name"])
        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
            w.writeheader()
            w.writerows(ordered)

    def work(d):
        url, method = (None, "none")
        try:
            url, method = resolve_staff_directory(
                get_fetcher(), d["website"], d["district_name"], config)
        except Exception as exc:
            logger.debug("resolve failed for %s: %s", d["district_name"], exc)
        return {
            "district_number": d["district_number"],
            "district_name": d["district_name"],
            "county": d["county"],
            "region": d["region"],
            "website": d["website"],
            "staff_directory_url": url or "",
            "method": method,
            "status": "found" if url else "not_found",
        }

    try:
        from tqdm import tqdm
        pbar = tqdm(total=len(sample), desc="Districts", unit="dist")
    except Exception:
        pbar = None

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, d) for d in sample]
        for fut in as_completed(futures):
            try:
                rec = fut.result()
                with lock:
                    results.append(rec)
            except Exception as exc:
                logger.debug("task failed: %s", exc)
            done += 1
            if pbar:
                pbar.update(1)
            if done % checkpoint_every == 0:
                with lock:
                    flush()
    if pbar:
        pbar.close()
    flush()

    found = sum(1 for r in results if r["status"] == "found")
    print("\n" + "=" * 66)
    print(f"DONE. {found}/{len(results)} districts got a staff-directory link "
          f"({found/len(results)*100:.0f}%).")
    print(f"Saved: {out_path.resolve()}")
    print("=" * 66)
    return str(out_path)


# ============================================================================
# STEP 4: TURN DIRECTORY LINKS INTO ACTUAL LEADS (people + emails)
# ============================================================================

LEAD_OUTPUT_FIELDS = ["district", "school", "first_name", "last_name",
                      "email", "role", "lead_type"]


def run_leads(directory_csv: str,
              output_csv: str = "texas_leads.csv",
              max_districts: int = 0,
              use_javascript: bool = False,
              verify_ssl: bool = False,
              follow_profiles: bool = True,
              max_schools: int = 25,
              rate_limit_delay: float = 0.3,
              checkpoint_every: int = 3,
              workers: int = 8) -> str:
    """Read the directory-links CSV produced by run(), then for each district
    discover its campuses and scrape every staff directory into a leads file.

    Speed: this work is network-bound (waiting on websites), so the lever is
    concurrency, not GPU/CPU. `workers` controls how many districts are scraped
    in parallel (threads). 8 is a good default; raise for more speed, lower to
    be gentler on sites. Each worker keeps its own polite per-host rate limit.

      * verify_ssl=False handles Texas school sites with broken certificates.
      * use_javascript=False (default) is faster and, in Colab, more reliable.
      * Progress is checkpointed to output_csv every `checkpoint_every`
        completed districts, so a dropped session keeps what's done.
    """
    import master_staff_directory_scraper as m
    import pandas as pd
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    m.setup_logging("INFO")

    src = pd.read_csv(directory_csv).drop_duplicates(
        "district_name").reset_index(drop=True)
    if max_districts and max_districts > 0:
        src = src.head(max_districts)
    logger.info("Districts to process: %d (workers=%d)", len(src), workers)

    cfg = m.ScraperConfig(
        respect_robots=True, rate_limit_delay=rate_limit_delay,
        use_javascript=use_javascript, verify_ssl=verify_ssl,
        request_timeout=15, directory_crawl_depth=1,
        max_directories_per_school=6)

    all_leads: List = []
    errors: List[str] = []
    out_path = Path(output_csv)
    lock = threading.Lock()

    # One Fetcher per worker thread (sessions aren't meant to be shared).
    _tls = threading.local()

    def get_fetcher():
        f = getattr(_tls, "fetcher", None)
        if f is None:
            f = _tls.fetcher = m.Fetcher(cfg)
        return f

    def flush():
        rows = []
        for e in m.deduplicate(all_leads):
            rows.append({
                "district": e.district, "school": e.school,
                "first_name": e.first_name, "last_name": e.last_name,
                "email": e.email, "role": e.title, "lead_type": e.lead_type,
            })
        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=LEAD_OUTPUT_FIELDS)
            w.writeheader()
            w.writerows(rows)

    def work(d):
        name = d.get("district_name", "")
        website = d.get("website", "")
        known = d.get("staff_directory_url") if str(
            d.get("status")) == "found" else None
        if not website:
            return name, []
        got = m.scrape_district_full(
            get_fetcher(), name, website, known, cfg,
            max_schools=max_schools, follow_profiles=follow_profiles)
        return name, got

    rows = [r._asdict() if hasattr(r, "_asdict") else dict(r)
            for r in src.itertuples(index=False)]

    try:
        from tqdm import tqdm
        pbar = tqdm(total=len(rows), desc="Districts", unit="dist")
    except Exception:
        pbar = None

    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = {ex.submit(work, d): d.get("district_name", "")
                       for d in rows}
            for fut in as_completed(futures):
                nm = futures[fut]
                try:
                    name, got = fut.result()
                    with lock:
                        all_leads.extend(got)
                    sc = len({e.school for e in got})
                    em = sum(1 for e in got if e.email)
                    logger.info("  %-30s %4d leads | %2d schools | %3d emails",
                                name[:30], len(got), sc, em)
                except Exception as exc:
                    errors.append(f"{nm}: {exc}")
                    logger.warning("  %-30s FAILED: %s", nm[:30], exc)
                done += 1
                if pbar:
                    pbar.update(1)
                if done % checkpoint_every == 0:
                    with lock:
                        flush()
    finally:
        if pbar:
            pbar.close()
        with lock:
            flush()

    leads = m.deduplicate(all_leads)
    n_email = sum(1 for e in leads if e.email)
    n_school = len({(e.district, e.school) for e in leads})
    print("\n" + "=" * 66)
    print(f"LEADS DONE. {len(leads)} leads across {n_school} schools.")
    print(f"With email: {n_email} ({(n_email/len(leads)*100 if leads else 0):.0f}%)")
    if errors:
        print(f"Districts with errors: {len(errors)} (see log)")
    print(f"Saved: {out_path.resolve()}")
    print("=" * 66)
    return str(out_path)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--askted-csv", default=None)
    p.add_argument("--fraction", type=float, default=0.30)
    p.add_argument("--output", default="texas_district_staff_directories.csv")
    p.add_argument("--js", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    a, _ = p.parse_known_args()
    run(askted_csv=a.askted_csv, sample_fraction=a.fraction,
        output_csv=a.output, use_javascript=a.js, limit=a.limit)