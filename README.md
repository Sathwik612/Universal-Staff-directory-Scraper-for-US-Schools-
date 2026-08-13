# School Staff-Directory Lead Scraper

Build leads (staff contacts) from public U.S. school-district staff directories.
Output schema, everywhere:

```
district | school | first_name | last_name | email | role | lead_type
```

`lead_type` ∈ {Dist, School, Teacher, Other}. Some outputs also carry
`department` and an `email_source` flag (`scraped` vs `constructed`).

---

## 1. The core idea

Don't scrape district-by-district. Districts run a small number of website
**vendors/CMS platforms**, and every district on the same platform serves its
directory the same way. So:

1. Get the state's district list + website URLs.
2. Find each district's staff-directory URL.
3. **Profile** each directory by CMS vendor + structure.
4. Scrape per vendor bucket (one extractor covers many districts).
5. Merge + dedupe.

**Decisive rule learned the hard way:** most districts deliberately *don't*
publish staff emails. No scraper can extract what isn't in the page. So the
honest high-value target is always: the districts that actually publish emails
(profile `n_mailto >= 3`) plus the vendors whose emails are hidden-but-present
(Finalsite). Vendors that route through a "Send Message" form (ParentSquare,
most Edlio, Webflow contact forms, login portals) are dead ends for email.

---

## 2. Pipeline stages

| Stage | Tool | What it does |
|-------|------|--------------|
| 0 | (state export) | Get district list + websites (and often free leadership emails) |
| 1 | `build_texas_staff_directory_links.run()` | Find each site's staff-directory URL |
| 2 | `profile_district_directories.profile_directories()` | Fingerprint CMS + `dir_style` + `n_mailto` |
| 3a | `build_texas_staff_directory_links.run_leads()` | Static scrape of email-publishing districts |
| 3b | `finalsite_scraper_async.run_finalsite()` | Browser scrape of Finalsite (hidden emails, pagination, profiles) |
| 3c | `foxbright_scraper.run_foxbright()` | Foxbright CMS (Michigan cluster) |
| 3d | `email_extractor.get_emails()` | One-off single-URL extractor for oddballs |
| 4 | `campus_pass.py` | Recover teachers for districts scraped admin-only |
| 5 | (merge script) | Concatenate + dedupe on district+email |

Note: `build_texas_staff_directory_links.py` is **state-agnostic** despite the
name — only the input CSV changes per state.

---

## 3. Files

**Core engine**
- `master_staff_directory_scraper.py` — HTTP layer (retries, `verify_ssl`
  toggle, `connect_timeout=8` so dead hosts fail fast, `skip_hosts`), CMS
  detection, school discovery, multi-strategy parsing (tables/cards/mailto/
  microdata/JSON/hCard), role inference, `classify_lead()`, email extraction
  (mailto + Cloudflare `data-cfemail` + "at/dot" + profile-follow). Nothing runs
  on import.
- `build_texas_staff_directory_links.py` — the pipeline. `run()` finds directory
  links (parallel, `workers=`). `run_leads()` scrapes people (parallel,
  checkpointed, static/no-browser). Fails loudly if the given CSV path is
  missing (won't silently auto-download Texas).
- `profile_district_directories.py` — `profile_directories()` fingerprints each
  directory (the vendor map).

**Vendor / special scrapers**
- `finalsite_scraper_async.py` — Finalsite (async Playwright + nest_asyncio).
  Handles hidden `fsStyleSROnly`/bare-href/JS emails, `const_page` direct-loop
  **and** click pagination (auto-detected, reads "showing X of Y"), dedupe by
  `data-constituent-id`, profile-following for new-page **and** modal patterns,
  `probe_paths=True` to find the real directory URL, department + school
  extraction. Entry: `run_finalsite(targets_csv, output_csv, districts=[substr],
  only_districts_in=[exact], only_cms_finalsite, concurrency, wait_ms,
  follow_profiles, probe_paths)`.
- `foxbright_scraper.py` — Foxbright CMS (`fbcms_staffentry` structure), static.
  `run_foxbright(targets_csv, output_csv, workers)`.
- `austin_isd_scraper.py` — model for a single huge district with a uniform
  campus template.
- `email_extractor.py` — standalone single-URL tool. `get_emails(url)` renders
  in a browser, clicks email icons, decodes mailto/bare-href/Cloudflare/at-dot/
  SR-only spans, paginates. `get_emails_static(url)` = fast no-browser version.
- `campus_pass.py` — `find_admin_only()` flags districts scraped admin-only;
  `build_campus_targets()` discovers their school subdomains for a campus pass.

**Superseded (ignore):** `finalsite_scraper.py`, `finalsite_scraper_js.py`,
`find_campus_gaps.py`.

---

## 4. Quick start (Colab)

```python
# once per session
!pip install -q playwright nest_asyncio beautifulsoup4 lxml pandas requests
!playwright install chromium
!playwright install-deps chromium
```
```python
import nest_asyncio; nest_asyncio.apply()
# put your files + <state>_districts.csv in the working dir (or mount Drive)
```

**Stage 1 → 2 → profile:**
```python
from build_texas_staff_directory_links import run, run_leads
run(askted_csv="<state>_districts.csv", sample_fraction=1.0,
    output_csv="<state>_directories.csv", workers=32)

from profile_district_directories import profile_directories
profile_directories("<state>_directories.csv",
                    output_csv="<state>_profiles.csv", workers=32)

import pandas as pd
p = pd.read_csv("<state>_profiles.csv")
print(p["cms"].value_counts()); print(pd.crosstab(p["cms"], p["dir_style"]))
```

**Static email-publishers (fast win):**
```python
p[p["n_mailto"]>=3].to_csv("<state>_email_districts.csv", index=False)
run_leads("<state>_email_districts.csv", output_csv="<state>_email_leads.csv",
          verify_ssl=False, workers=16)
```

**Finalsite bucket:**
```python
from finalsite_scraper_async import run_finalsite
# test a few first, then:
run_finalsite("<state>_profiles.csv", output_csv="<state>_finalsite.csv",
              only_cms_finalsite=True, concurrency=3, wait_ms=2000,
              follow_profiles=False, probe_paths=True)
```

## 4b. Running locally (VS Code) — recommended for big runs

Local avoids Colab timeouts/disconnects and allows higher `workers`. A GPU does
**not** help — this is network-bound, not compute-bound. The speed lever is
`workers` (threads), not hardware.

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install pandas beautifulsoup4 lxml requests playwright nest_asyncio openpyxl tqdm
playwright install chromium
python your_script.py       # e.g. calls run(...) with workers=64
```

---

## 5. Getting the district list per state (Stage 0)

Each state has an official directory export — the equivalent of Texas AskTED:

- **Texas** — AskTED (auto-downloadable).
- **Ohio** — OEDS (`oeds.ode.state.oh.us/DataExtract`). Had websites **and**
  superintendent/treasurer/org emails → 958 free leads.
- **Michigan** — EEM public datasets (`cepi.state.mi.us/EEM/PublicDatasets.aspx`).
  No website column, but the admin **email domain** yields the website for 99%
  of districts, plus 807 free admin emails.
- **North Carolina** — EDDIE (NCDPI). Names only in the file we had; websites
  need resolving.
- **Arizona / Maryland** — district lists came with websites/domains directly.

**Trick that worked well (Michigan):** when the export has admin emails but no
website, derive the website from the email domain (`jdoe@alleganps.org` →
`alleganps.org`). Also harvest those admin emails as free leads.

---

## 6. Results by state (what actually worked)

| State | Outcome | Notes |
|-------|---------|-------|
| **Texas** | Done (Austin + Finalsite + 65 email-publishers) | Campus-level pass on 8,986 schools in progress (local) |
| **Ohio** | ~24k emails (20,082 scraped + ~4k constructed Cleveland) | Finalsite bucket huge; 958 free OEDS leads; profile-recovery on big districts |
| **Michigan** | 807 free admin + 1,163 static + Foxbright cluster (84 districts) | "Finalsite" bucket was mislabeled Webflow (contact-form, no email); Foxbright is the real cluster |
| **Arizona** | 771 static + ~4,400 Finalsite (list-emails) | Finalsite strong (Marana 1,979, Pendergast 1,199); some profile-hidden + big 0-staff districts |
| **Maryland** | ~120 real (Carroll) | Locked-down state: portals, ParentSquare, contact-forms. Low yield — correctly stopped early |

**Vendor cheat-sheet (holds across states):**
- **Finalsite** — emails hidden but present (SR-only span / bare href / JS /
  profile). Crackable. `probe_paths=True` finds the real directory URL;
  `const_page` pagination; `follow_profiles=True` for profile-hidden ones.
- **ParentSquare (SmartSites)** — "Send Message" form, email never in HTML. Dead.
- **Edlio** — mostly profile-hidden, few emails. Low value.
- **Foxbright** (Michigan) — `fbcms_staffentry` cards; some show emails, some
  don't. Static, fast.
- **Webflow / login portals** — contact-form or gated. Dead for email.

---

## 7. Constructing emails (when scraping can't get them)

For big districts where names are public but emails are profile-hidden (e.g.
Cleveland, AZ profile-hidden districts), you can **construct** emails from a
known pattern, then **verify** a sample against real profiles:

- Patterns vary per district: `first.last@`, `flast@`, `f_last@`, sometimes with
  a numeric suffix for collisions (`saunders.5@`), sometimes a second domain.
- Always flag constructed emails `email_source="constructed"` — they are guesses
  and some will bounce. Verify the match rate on ~24-48 real profiles before
  trusting a district's constructed set.

---

## 8. Merge + dedupe (final step per state)

```python
import pandas as pd, os
cols = ["district","school","first_name","last_name","email","role","lead_type"]
parts = []
for f in [<all your per-bucket csvs>]:
    if os.path.exists(f):
        x = pd.read_csv(f)
        for c in cols:
            if c not in x.columns: x[c] = ""
        x = x[cols].copy(); x["email_source"] = "scraped"   # or "constructed"
        parts.append(x)
allo = pd.concat(parts, ignore_index=True)
allo["email"] = allo["email"].astype(str).str.lower().str.strip()
allo = allo[allo["email"].str.contains("@", na=False)]
allo = allo.drop_duplicates(subset=["email"])
allo.to_csv("<state>_ALL_leads.csv", index=False)
```

---

## 9. Operational lessons (all learned the hard way)

- **File location is the #1 recurring bug.** `run()` needs the CSV in the
  working directory; if not found it auto-downloads **Texas**. Always confirm
  `os.path.exists(...)` and watch for "Districts parsed: 2306" vs 1213.
- **Colab wipes files on reset** → mount Drive, work inside the Drive folder,
  save outputs there. Chromium must be reinstalled every fresh runtime.
- **The ~95% "hang"** is dead/slow hosts in the tail. `connect_timeout=8` +
  `connect=1` retry fixes most of it; otherwise let it reach ~95% and interrupt
  (everything checkpoints).
- **OOM crashes** come from too many browsers. Finalsite `concurrency=2-3` max;
  drop to 1 on giant districts. `follow_profiles=False` is the big speed/RAM win
  when emails are on the list.
- **Playwright**: async API + `nest_asyncio.apply()`; `ignore_https_errors=True`;
  `wait_until="domcontentloaded"`.
- **`workers`**: static 32 (up to 64 locally); browser `concurrency` 2-4.
  GPU does nothing (network-bound).
- **`sys.modules.pop("modname", None)`** before re-importing an edited file.
- **Verify before building.** For any hidden-email vendor, inspect one page
  (right-click → Inspect, Ctrl+F for `@`) before writing an extractor. Saved
  huge time on ParentSquare/Edlio (confirmed dead) and Foxbright (found the
  real structure).

---

## 10. Compliance

This assembles real people's contact data from public directories. Before
outreach, confirm each site's Terms/robots.txt and applicable law (CAN-SPAM,
state privacy rules). Not legal advice.

---

## Author

**Sathwik N H**
- Email: sathwiknh@gmail.com
- LinkedIn: https://www.linkedin.com/in/sathwiknh1/
