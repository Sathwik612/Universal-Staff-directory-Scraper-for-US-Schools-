#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CAMPUS PASS — capture teachers for districts whose district-level directory
only held central-office/admin staff (the teachers live on per-school
subdomains, e.g. ahs.athenscsd.org, pinnacle.pvschools.net).

Two steps:
  1. find_admin_only(): from a leads CSV, flag districts that returned few staff
     (likely admin-only) and thus need a campus pass.
  2. build_campus_targets(): for those districts, discover their school
     subdomains/pages from the district homepage, and emit a targets CSV of
     per-campus staff-directory URLs to feed run_finalsite(..., probe_paths=True).

Run in Colab (needs master_staff_directory_scraper on path).

USAGE:
    from campus_pass import find_admin_only, build_campus_targets

    gaps = find_admin_only("ohio_ALL_leads.csv",
                           districts_csv="ohio_districts.csv",
                           admin_threshold=40)

    build_campus_targets(gaps, districts_csv="ohio_districts.csv",
                         output_csv="ohio_campus_targets.csv")

    # then, with the Finalsite scraper:
    from finalsite_scraper_async import run_finalsite
    run_finalsite("ohio_campus_targets.csv", output_csv="ohio_campus_leads.csv",
                  districts=<the campus names>, concurrency=3, wait_ms=2000,
                  follow_profiles=True, probe_paths=True)
"""
from __future__ import annotations
import re
import csv
import logging
from urllib.parse import urlparse

import pandas as pd

logger = logging.getLogger("campus_pass")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    datefmt="%H:%M:%S")


def find_admin_only(leads_csv: str, districts_csv: str | None = None,
                    admin_threshold: int = 40) -> pd.DataFrame:
    """Flag districts whose captured staff count is small enough to be
    admin-only. Returns a DataFrame of those districts + their website."""
    df = pd.read_csv(leads_csv)
    if "district" not in df.columns:
        raise SystemExit("leads_csv needs a 'district' column")
    per = df.groupby("district").agg(
        staff=("first_name", "size"),
        emails=("email", lambda s: s.astype(str).str.contains("@").sum()),
    ).reset_index()
    gaps = per[per["staff"] <= admin_threshold].sort_values("staff")

    # attach website if we have the district list
    if districts_csv:
        d = pd.read_csv(districts_csv)
        namecol = next((c for c in d.columns if "name" in c.lower()), None)
        webcol = next((c for c in d.columns if "web" in c.lower()
                       or "url" in c.lower()), None)
        if namecol and webcol:
            d = d[[namecol, webcol]].rename(
                columns={namecol: "district", webcol: "website"})
            # fuzzy-ish join on lowercased name
            gaps["_k"] = gaps["district"].str.lower().str.strip()
            d["_k"] = d["district"].str.lower().str.strip()
            gaps = gaps.merge(d[["_k", "website"]], on="_k", how="left").drop(
                columns="_k")

    print(f"Districts likely ADMIN-ONLY (<= {admin_threshold} staff): "
          f"{len(gaps)}")
    print(gaps.to_string(index=False))
    return gaps


def build_campus_targets(gaps: pd.DataFrame, districts_csv: str | None = None,
                         output_csv: str = "campus_targets.csv",
                         verify_ssl: bool = False, workers: int = 12):
    """For each admin-only district, discover its school subdomains/pages and
    write per-campus staff-directory candidate URLs."""
    import sys
    import importlib
    m = importlib.import_module("master_staff_directory_scraper")
    Fetcher = m.Fetcher
    ScraperConfig = m.ScraperConfig
    discover_schools = m.discover_schools

    cfg = ScraperConfig()
    try:
        cfg.verify_ssl = verify_ssl
    except Exception:
        pass
    fetcher = Fetcher(cfg)

    rows = []
    for _, r in gaps.iterrows():
        district = r["district"]
        website = str(r.get("website", "") or "").strip()
        if not website:
            logger.info("  %-40s (no website, skipped)", district[:40])
            continue
        if not website.startswith("http"):
            website = "https://" + website
        try:
            schools = discover_schools(fetcher, website, district)
        except Exception as exc:
            logger.info("  %-40s discover failed: %s", district[:40], exc)
            schools = []
        logger.info("  %-40s -> %d campuses", district[:40], len(schools))
        for s in schools:
            url = getattr(s, "url", None) or getattr(s, "website", None) or ""
            name = getattr(s, "name", "") or ""
            if not url:
                continue
            # candidate staff-directory paths appended by run_finalsite's probe
            rows.append({
                "district_name": f"{district} — {name}",
                "staff_directory_url": url,
                "cms": "finalsite",   # probing will confirm/deny
            })

    with open(output_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "district_name", "staff_directory_url", "cms"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} campus targets across "
          f"{gaps['district'].nunique()} districts -> {output_csv}")
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    leads = sys.argv[1] if len(sys.argv) > 1 else "ohio_ALL_leads.csv"
    find_admin_only(leads)
