#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
MASTER STAFF DIRECTORY SCRAPER
================================================================================
A single, consolidated, robust scraper for school / organization staff
directories.

This module replaces an older file that had accumulated four different scraper
versions concatenated together. It keeps the good ideas from all of them:

  * Tiered role inference (name credentials -> explicit title -> context ->
    URL as a last resort) so people are not all mislabelled by the directory
    section they happened to appear in.
  * Automatic name/title column swap-fixing and "Email " prefix cleaning.
  * Multiple parsing strategies tried in order: platform-specific (Finalsite),
    embedded JSON / JSON-LD, HTML tables, employee "cards", then a mailto
    fallback.
  * CMS detection (Finalsite, Blackboard, Edlio, Apptegy, SchoolBlocks, ...).
  * School discovery from homepage navigation and sitemap.xml (generalized,
    not hardcoded to any one district).
  * Optional JavaScript rendering via Playwright, used ONLY as a fallback and
    degrading gracefully to plain HTTP when Playwright is not installed.

Robustness features:
  * Retries with exponential backoff and connection pooling.
  * Per-host rate limiting and polite delays.
  * robots.txt awareness (on by default; can be disabled).
  * Nothing runs on import. Use the functions below or the CLI.

USAGE (as a library)
--------------------
    from master_staff_directory_scraper import (
        scrape_directory_page, scrape_district, ScraperConfig,
    )

    # A) One directory page:
    result = scrape_directory_page(
        "https://www.school.edu/staff-directory",
        school_name="Lincoln High School",
    )

    # B) A whole district (discovers schools, finds each directory):
    result = scrape_district("https://www.example.org", "Example District")

    show_stats(result)
    df = get_dataframe(result)

USAGE (command line)
--------------------
    python master_staff_directory_scraper.py \
        --url https://www.example.org --name "Example District"

    python master_staff_directory_scraper.py \
        --url https://www.school.edu/staff --name "Lincoln HS" --single-page

    # Enable JS rendering fallback (requires: pip install playwright &&
    # playwright install chromium):
    python master_staff_directory_scraper.py --url ... --js

Optional dependency install helper:
    python master_staff_directory_scraper.py --install
================================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

# --- Third-party imports (hard dependencies) --------------------------------
try:
    import requests
    from requests.adapters import HTTPAdapter
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - guidance only
    raise SystemExit(
        "Missing required package: %s\n"
        "Install core dependencies with:\n"
        "    pip install requests beautifulsoup4 lxml pandas openpyxl\n"
        "or run:  python master_staff_directory_scraper.py --install"
        % exc.name
    )

try:  # urllib3 Retry lives in different places across versions
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

# pandas is only needed for export/stats; import lazily so the parser itself
# works without it.
try:
    import pandas as pd
    _HAS_PANDAS = True
except Exception:  # pragma: no cover
    pd = None  # type: ignore
    _HAS_PANDAS = False


# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger("staff_scraper")


def setup_logging(level: str = "INFO") -> None:
    """Configure logging. Safe to call multiple times."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                              datefmt="%H:%M:%S")
        )
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


# ============================================================================
# ENUMS
# ============================================================================

class CMSType(Enum):
    FINALSITE = "finalsite"
    BLACKBOARD = "blackboard"
    EDLIO = "edlio"
    APPTEGY = "apptegy"
    SCHOOLBLOCKS = "schoolblocks"
    GABBART = "gabbart"
    PARENTSQUARE = "parentsquare"
    SQUARESPACE = "squarespace"
    WORDPRESS = "wordpress"
    UNKNOWN = "unknown"


class EmployeeType(Enum):
    ADMINISTRATION = "administration"
    TEACHER = "teacher"
    SUPPORT_STAFF = "support_staff"
    HEALTH_SERVICES = "health_services"
    ATHLETICS = "athletics"
    OTHER = "other"


# Ordered list used consistently by exporters so all four old versions'
# category sets are covered.
EMPLOYEE_TYPE_ORDER = [
    "administration", "teacher", "support_staff",
    "health_services", "athletics", "other",
]


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class Employee:
    district: str
    school: str
    name: str
    title: str = "Staff"
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    extension: Optional[str] = None
    department: Optional[str] = None
    room: Optional[str] = None
    office: Optional[str] = None
    subjects: List[str] = field(default_factory=list)
    grade: Optional[str] = None
    biography: Optional[str] = None
    photo_url: Optional[str] = None
    profile_url: Optional[str] = None
    school_url: Optional[str] = None
    directory_url: Optional[str] = None
    employee_type: EmployeeType = EmployeeType.OTHER
    lead_type: str = "Other"          # Dist | School | Teacher | Other
    scraped_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "district": self.district,
            "school": self.school,
            "name": self.name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "title": self.title,
            "department": self.department,
            "email": self.email,
            "phone": self.phone,
            "extension": self.extension,
            "room": self.room,
            "office": self.office,
            "subjects": "; ".join(self.subjects) if self.subjects else None,
            "grade": self.grade,
            "biography": self.biography,
            "photo_url": self.photo_url,
            "profile_url": self.profile_url,
            "school_url": self.school_url,
            "directory_url": self.directory_url,
            "employee_type": self.employee_type.value,
            "lead_type": self.lead_type,
            "scraped_at": self.scraped_at.isoformat(),
        }


@dataclass
class School:
    name: str
    url: str
    district: str = ""
    district_url: str = ""
    cms_type: CMSType = CMSType.UNKNOWN
    directory_urls: List[str] = field(default_factory=list)
    employee_count: int = 0
    error: Optional[str] = None


@dataclass
class ScraperConfig:
    """All tunable behavior lives here."""
    request_timeout: int = 30
    max_retries: int = 3
    retry_backoff_factor: float = 1.0
    rate_limit_delay: float = 0.4          # polite delay between requests / host
    max_schools: int = 0                   # 0 = no limit
    max_directories_per_school: int = 8
    directory_crawl_depth: int = 1
    respect_robots: bool = True
    use_javascript: bool = False           # enable Playwright fallback
    verify_ssl: bool = True                 # set False for sites w/ bad certs
    js_wait_ms: int = 2000
    output_dir: str = "output"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


# ============================================================================
# TEXT / NORMALIZATION UTILITIES
# ============================================================================

_WS_RE = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_PHONE_IN_TEXT_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")

# Credentials that identify health-services staff, checked against the name.
_NURSE_CREDENTIALS = {
    "RN": ("Registered Nurse (RN)", "Health Services"),
    "BSN": ("Registered Nurse (BSN)", "Health Services"),
    "LVN": ("Licensed Vocational Nurse (LVN)", "Health Services"),
    "LPN": ("Licensed Practical Nurse (LPN)", "Health Services"),
    "NP": ("Nurse Practitioner", "Health Services"),
}


def clean_text(text: Optional[str]) -> str:
    """Collapse whitespace and strip."""
    if not text:
        return ""
    return _WS_RE.sub(" ", str(text)).strip()


def clean_email_prefix(text: Optional[str]) -> Optional[str]:
    """Remove leading label noise some directories prepend, e.g. 'Email ',
    'Titles: ', 'Name: '."""
    if text is None:
        return None
    t = clean_text(text)
    low = t.lower()
    for pref in ("email ", "e-mail ", "titles:", "title:", "name:", "role:",
                 "position:"):
        if low.startswith(pref):
            t = t[len(pref):].strip()
            low = t.lower()
    return t


# Country / territory names (lowercased) plus junk patterns. Staff directories
# frequently contain a <select> country dropdown that naive parsers mistake for
# people ("Antigua & Barbuda", "Congo - Kinshasa", ...). We hard-reject these.
_COUNTRY_RAW = (
    "afghanistan albania algeria andorra angola anguilla argentina armenia "
    "aruba australia austria azerbaijan bahamas bahrain bangladesh barbados "
    "belarus belgium belize benin bermuda bhutan bolivia botswana brazil brunei "
    "bulgaria burundi cambodia cameroon canada chad chile china colombia "
    "comoros croatia cuba curacao cyprus denmark djibouti dominica ecuador "
    "egypt eritrea estonia eswatini ethiopia fiji finland france gabon gambia "
    "georgia germany ghana gibraltar greece greenland grenada guadeloupe guam "
    "guatemala guernsey guinea guyana haiti honduras hungary iceland india "
    "indonesia iran iraq ireland israel italy jamaica japan jersey jordan "
    "kazakhstan kenya kiribati kosovo kuwait kyrgyzstan laos latvia lebanon "
    "lesotho liberia libya liechtenstein lithuania luxembourg macao madagascar "
    "malawi malaysia maldives mali malta martinique mauritania mauritius "
    "mayotte mexico micronesia moldova monaco mongolia montenegro montserrat "
    "morocco mozambique myanmar namibia nauru nepal netherlands nicaragua "
    "niger nigeria niue norway oman pakistan palau panama paraguay peru "
    "philippines poland portugal qatar romania rwanda samoa senegal serbia "
    "seychelles singapore slovakia slovenia somalia spain sudan suriname sweden "
    "switzerland syria taiwan tajikistan tanzania thailand togo tokelau tonga "
    "tunisia turkey turkmenistan tuvalu uganda ukraine uruguay uzbekistan "
    "vanuatu venezuela vietnam yemen zambia zimbabwe"
)
_COUNTRY_NAMES = frozenset(_COUNTRY_RAW.split())


_JUNK_NAME_RE = re.compile(
    r"(&|\bislands?\b|\brepublic\b|\bterritory\b|\bcity\b|"
    r"last names?\s+[a-z]\s*-\s*[a-z]|\ba-[a-z]\b|[0-9])", re.IGNORECASE)


def is_junk_name(name: Optional[str]) -> bool:
    """True if a 'name' is actually a dropdown option, section header, or
    other non-person junk."""
    if not name:
        return True
    n = clean_text(name)
    if not n:
        return True
    nl = n.lower()
    if nl in _COUNTRY_NAMES:
        return True
    # Country phrases like "antigua & barbuda", "congo - kinshasa"
    base = re.split(r"\s*[&(\-]", nl)[0].strip()
    if base in _COUNTRY_NAMES:
        return True
    if _JUNK_NAME_RE.search(n):
        return True
    return False


# Professional / academic credential tokens that may trail a name after a
# comma (e.g. "Jane Doe, RN, BSN"). These are NOT surnames and must be
# stripped before deciding first/last, or "Last, First" parsing misfires.
_CREDENTIAL_TOKENS = {
    "RN", "LVN", "LPN", "BSN", "MSN", "NP", "APRN", "CNA", "PHD", "EDD", "MD",
    "DDS", "DVM", "PSYD", "MED", "MSED", "MBA", "JD", "CPA", "NBCT", "LCSW",
    "LPC", "LMSW", "LMFT", "OTR", "PT", "DPT", "SLP", "CCCSLP", "EDS", "ABD",
    "BCBA", "ATC", "RD", "LDN",
}


_HONORIFICS = {"DR", "MR", "MRS", "MS", "MX", "PROF", "PROFESSOR", "REV",
               "COACH", "FR", "SR", "HON", "SGT", "OFC", "OFFICER"}


def _is_credential_chunk(chunk: str) -> bool:
    """True if every whitespace token in a comma-chunk is a known credential."""
    toks = [re.sub(r"[.\s]", "", t).upper() for t in chunk.split() if t.strip()]
    return bool(toks) and all(t in _CREDENTIAL_TOKENS for t in toks)


def _drop_leading_honorific(tokens: List[str]) -> List[str]:
    """Remove a leading honorific token (Dr., Mr., etc.) when a real name
    still follows it."""
    if len(tokens) >= 2:
        head = re.sub(r"[.\s]", "", tokens[0]).upper()
        if head in _HONORIFICS:
            return tokens[1:]
    return tokens


def normalize_name(name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Split a display name into (first, last).

    Handles "First Last", "Last, First", and trailing credentials such as
    "Quinn Adebayo, LVN" or "Jane Doe, RN, BSN".
    """
    if not name:
        return None, None
    n = clean_text(name)
    if not n:
        return None, None

    parts = [p.strip() for p in n.split(",") if p.strip()]
    # Drop trailing credential chunks (never the first chunk).
    while len(parts) > 1 and _is_credential_chunk(parts[-1]):
        parts.pop()

    if len(parts) >= 2:
        # "Last, First [Middle ...]"
        last = parts[0]
        first_tokens = _drop_leading_honorific(parts[1].split())
        first = first_tokens[0] if first_tokens else None
        return first, last

    # Single chunk: "First [Middle] Last"
    tokens = _drop_leading_honorific(parts[0].split()) if parts else []
    if not tokens:
        return None, None
    if len(tokens) == 1:
        return tokens[0], None
    return tokens[0], " ".join(tokens[1:])


def normalize_phone(phone: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (formatted_phone, extension). Falls back to the cleaned input
    when it can't confidently parse 10 digits."""
    if not phone:
        return None, None
    raw = str(phone)
    extension = None
    ext_match = re.search(r"(?:ext|extension|x)\.?\s*(\d+)", raw, re.IGNORECASE)
    if ext_match:
        extension = ext_match.group(1)
        raw = raw[: ext_match.start()]
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        cleaned = clean_text(raw)
        return (cleaned or None), extension
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", extension


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Validate + lowercase an email; strip mailto and query params."""
    if not email:
        return None
    e = str(email).strip()
    if e.lower().startswith("mailto:"):
        e = e[7:]
    e = e.split("?")[0].strip().lower()
    return e if _EMAIL_RE.match(e) else None


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def get_root_domain(url: str) -> str:
    """example.k12.tx.us -> k12.tx.us style is hard in general; we return the
    last two labels which is a reasonable default for most .com/.org/.edu."""
    host = get_domain(url)
    host = host.split(":")[0]
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


_SCHOOL_WORDS = (
    "elementary", "middle school", "high school", "academy", "preparatory",
    "collegiate", "primary", "secondary", "intermediate", "preschool",
    "early childhood", "magnet", "charter", "learning center", "school of",
    "junior high", "senior high", "campus",
)
_NON_PERSON_WORDS = (
    "cost:", "click here", "read more", "learn more", "$", "©", "copyright",
    "all rights reserved",
)
# Occupation / role words. A string containing one of these as a standalone
# word is a job title, not a person's name -- which is what lets us detect and
# fix directories that put the title where the name should go.
_ROLE_WORDS = frozenset((
    "teacher", "principal", "superintendent", "director", "coordinator",
    "counselor", "counselors", "librarian", "nurse", "coach", "coaches",
    "secretary", "clerk", "custodian", "janitor", "maintenance", "aide",
    "aides", "paraprofessional", "instructor", "professor", "dean",
    "supervisor", "manager", "administrator", "specialist", "assistant",
    "receptionist", "registrar", "psychologist", "therapist", "staff",
    "faculty", "department", "office", "services", "driver", "technician",
    "trustee", "president", "officer", "cafeteria", "security",
))


def _contains_role_word(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower())
    return any(w in _ROLE_WORDS for w in words)


def looks_like_person(text: Optional[str]) -> bool:
    """Heuristic: does this string look like a real person's name (as opposed
    to a school name, a title, or junk)?"""
    if not text:
        return False
    t = clean_text(text)
    if not t:
        return False
    tl = t.lower()
    if any(w in tl for w in _SCHOOL_WORDS):
        return False
    if any(w in tl for w in _NON_PERSON_WORDS):
        return False
    if _contains_role_word(t):
        return False
    if not re.search(r"[A-Za-z]", t):
        return False
    words = [w for w in t.replace(",", " ").split() if w]
    if len(words) < 2:
        return False
    # Reject obviously non-name strings that are all-caps sentences etc.
    if len(t) > 60:
        return False
    return True


# ============================================================================
# ROLE INFERENCE
# ============================================================================
# Priority (important): name credentials -> explicit title keywords ->
# school/context -> directory URL (last resort). This ordering is what keeps a
# whole "health-services" page from labelling everyone a nurse, etc.

_SUBJECT_MAP = {
    "math": ("Math Teacher", "Mathematics"),
    "science": ("Science Teacher", "Science"),
    "biology": ("Science Teacher", "Science"),
    "chemistry": ("Science Teacher", "Science"),
    "physics": ("Science Teacher", "Science"),
    "english": ("English Teacher", "English/Language Arts"),
    "language arts": ("English Teacher", "English/Language Arts"),
    "reading": ("Reading Teacher", "Reading"),
    "social studies": ("Social Studies Teacher", "Social Studies"),
    "history": ("History Teacher", "History"),
    "geography": ("Social Studies Teacher", "Social Studies"),
    "art": ("Art Teacher", "Fine Arts"),
    "music": ("Music Teacher", "Fine Arts"),
    "band": ("Band Director", "Fine Arts"),
    "orchestra": ("Orchestra Director", "Fine Arts"),
    "choir": ("Choir Director", "Fine Arts"),
    "theater": ("Theater Teacher", "Fine Arts"),
    "theatre": ("Theater Teacher", "Fine Arts"),
    "physical education": ("PE Teacher", "Physical Education"),
    "pe teacher": ("PE Teacher", "Physical Education"),
    "special education": ("Special Education Teacher", "Special Education"),
    "sped": ("Special Education Teacher", "Special Education"),
    "esl": ("ESL Teacher", "ESL"),
    "esol": ("ESL Teacher", "ESL"),
    "bilingual": ("Bilingual Teacher", "Bilingual"),
    "kindergarten": ("Kindergarten Teacher", "Elementary"),
    "pre-k": ("Pre-K Teacher", "Early Childhood"),
    "prek": ("Pre-K Teacher", "Early Childhood"),
    "computer": ("Computer/Technology Teacher", "Technology"),
    "career": ("Career/Technology Teacher", "CTE"),
}


def _match_subject(text: str) -> Optional[Tuple[str, str]]:
    for kw, val in _SUBJECT_MAP.items():
        if kw in text:
            return val
    return None


def infer_role(name: Optional[str], title: Optional[str], email: Optional[str],
               directory_url: Optional[str], school: Optional[str]
               ) -> Tuple[str, str, EmployeeType]:
    """Return (role_label, department, EmployeeType)."""
    name_str = clean_text(name)
    title_str = clean_text(title)
    email_str = (email or "").lower()
    dir_url = (directory_url or "").lower()
    school_str = (school or "").lower()

    name_upper = name_str.upper()
    tl = title_str.lower()

    # ---- TIER 1: credentials embedded in the name ----------------------
    for cred, (role, dept) in _NURSE_CREDENTIALS.items():
        # match ", RN" or trailing " RN" as a whole word
        if re.search(rf"(?:,\s*|\s){cred}\b", name_upper):
            return role, dept, EmployeeType.HEALTH_SERVICES

    # ---- TIER 2: explicit title keywords -------------------------------
    if tl and tl not in ("staff", "nan", "none"):
        if "superintendent" in tl:
            if "assistant" in tl or "deputy" in tl or "associate" in tl:
                return "Assistant Superintendent", "Administration", EmployeeType.ADMINISTRATION
            return "Superintendent", "Administration", EmployeeType.ADMINISTRATION
        if "principal" in tl:
            if any(w in tl for w in ("assistant", "asst", "vice", "associate")):
                return "Assistant Principal", "Administration", EmployeeType.ADMINISTRATION
            return "Principal", "Administration", EmployeeType.ADMINISTRATION
        if "dean" in tl:
            return "Dean", "Administration", EmployeeType.ADMINISTRATION
        # Music/fine-arts "directors" are instructional, not administrative --
        # check them before the generic director rule below.
        if any(w in tl for w in ("band director", "choir director",
                                 "orchestra director", "fine arts director",
                                 "theater director", "theatre director")):
            subj = _match_subject(tl)
            if subj:
                return subj[0], subj[1], EmployeeType.TEACHER
            return "Fine Arts Director", "Fine Arts", EmployeeType.TEACHER
        if "director" in tl:
            return "Director", "Administration", EmployeeType.ADMINISTRATION
        if "coordinator" in tl:
            return "Coordinator", "Administration", EmployeeType.ADMINISTRATION
        if "supervisor" in tl:
            return "Supervisor", "Administration", EmployeeType.ADMINISTRATION
        if "chief" in tl:
            return "Chief Officer", "Administration", EmployeeType.ADMINISTRATION
        if "manager" in tl:
            return "Manager", "Administration", EmployeeType.ADMINISTRATION
        if "board" in tl or "trustee" in tl:
            return "Board Member", "Board of Trustees", EmployeeType.ADMINISTRATION
        if "counselor" in tl or "counseling" in tl:
            return "Counselor", "Counseling", EmployeeType.SUPPORT_STAFF
        if "psycholog" in tl:
            return "School Psychologist", "Student Services", EmployeeType.SUPPORT_STAFF
        if "librarian" in tl or "media specialist" in tl:
            return "Librarian", "Library", EmployeeType.SUPPORT_STAFF
        if "nurse" in tl:
            return "Nurse", "Health Services", EmployeeType.HEALTH_SERVICES
        if "coach" in tl or "athletic" in tl:
            return "Coach/Athletics", "Athletics", EmployeeType.ATHLETICS
        if any(w in tl for w in ("teacher", "instructor", "educator",
                                 "professor", "lecturer", "faculty")):
            subj = _match_subject(tl)
            if subj:
                return subj[0], subj[1], EmployeeType.TEACHER
            return "Teacher", "Instruction", EmployeeType.TEACHER
        if "secretary" in tl or "clerk" in tl or "registrar" in tl:
            return "Secretary/Clerk", "Administrative Support", EmployeeType.SUPPORT_STAFF
        if "aide" in tl or "paraprofessional" in tl or "para-" in tl:
            return "Paraprofessional/Aide", "Instructional Support", EmployeeType.SUPPORT_STAFF
        if "custodian" in tl or "janitor" in tl:
            return "Custodian", "Facilities", EmployeeType.SUPPORT_STAFF
        if "maintenance" in tl:
            return "Maintenance", "Facilities", EmployeeType.SUPPORT_STAFF
        if "security" in tl or "police" in tl or "officer" in tl:
            return "Security", "Security", EmployeeType.SUPPORT_STAFF
        if any(w in tl for w in ("cafeteria", "food service", "nutrition")):
            return "Food Service", "Nutrition Services", EmployeeType.SUPPORT_STAFF
        if "bus driver" in tl or "transportation" in tl:
            return "Transportation Staff", "Transportation", EmployeeType.SUPPORT_STAFF
        if "technolog" in tl or "technician" in tl or "it " in tl:
            return "Technology Staff", "Technology", EmployeeType.SUPPORT_STAFF
        # A specific title we don't have a rule for: keep it, mark OTHER.
        return title_str, "General Staff", EmployeeType.OTHER

    # ---- TIER 3: school context ----------------------------------------
    if "health" in school_str and "profession" in school_str:
        return "Health Professions Staff", "Health Services", EmployeeType.HEALTH_SERVICES

    # ---- TIER 4: directory URL (only reached when title was generic) ---
    if "nurse" in dir_url or "health-service" in dir_url or "health_service" in dir_url:
        return "Health Services Staff", "Health Services", EmployeeType.HEALTH_SERVICES
    if "coach" in dir_url or "athletic" in dir_url:
        return "Coach/Athletics", "Athletics", EmployeeType.ATHLETICS
    if "principal" in dir_url or "administration" in dir_url or "admin" in dir_url:
        return "Administrator", "Administration", EmployeeType.ADMINISTRATION
    if "counselor" in dir_url or "counseling" in dir_url:
        return "Counselor", "Counseling", EmployeeType.SUPPORT_STAFF
    if "librar" in dir_url:
        return "Librarian", "Library", EmployeeType.SUPPORT_STAFF
    subj = _match_subject(dir_url)
    if subj:
        return subj[0], subj[1], EmployeeType.TEACHER
    if "teacher" in dir_url or "faculty" in dir_url:
        return "Teacher", "Instruction", EmployeeType.TEACHER

    # ---- TIER 5: fallback ----------------------------------------------
    if "@" in email_str:
        return "District Employee", "General Staff", EmployeeType.OTHER
    return "Staff", "General Staff", EmployeeType.OTHER


# ============================================================================
# LEAD CLASSIFICATION  (Dist / School / Teacher / Other)
# ============================================================================
# This is the "type of lead" bucket for outreach. The important, and hardest,
# distinction is district-level vs school-level decision makers, because many
# admin titles ("Director", "Coordinator") exist at BOTH levels. We resolve
# those ambiguous titles using the record's scope (was it scraped from the
# district as a whole, or from a specific campus?).

_LEAD_TEACHER_WORDS = ("teacher", "instructor", "educator", "professor",
                       "lecturer", "faculty")
_LEAD_SCHOOL_LEADER_WORDS = ("principal", "head of school", "headmaster",
                             "headmistress", "head teacher", "dean of students",
                             "dean")
_LEAD_ADMIN_WORDS = ("director", "coordinator", "supervisor", "manager",
                     "chief", "administrator", "executive", "cabinet",
                     "officer")
_DISTRICT_SCOPE_HINTS = ("district", "central office", "administration building",
                         "central administration", "district office",
                         "board of education", "central services", "d.o.")


def _is_district_scope(district: str, school: str) -> bool:
    """Heuristic: does this record belong to the district as a whole (rather
    than one campus)? True when there is no distinct campus name, when the
    'school' equals the district, or when it names a district-level office."""
    school_l = (school or "").strip().lower()
    district_l = (district or "").strip().lower()
    if not school_l:
        return True
    if district_l and school_l == district_l:
        return True
    return any(h in school_l for h in _DISTRICT_SCOPE_HINTS)


def classify_lead(title: Optional[str], role: Optional[str],
                  employee_type: EmployeeType,
                  district: str = "", school: str = "") -> str:
    """Return one of: 'Dist', 'School', 'Teacher', 'Other'."""
    text = f"{title or ''} {role or ''}".lower()
    district_scope = _is_district_scope(district, school)

    # 1) Unambiguous district leadership --------------------------------
    #    superintendent (incl. assistant/deputy/associate), board/trustees,
    #    or anything explicitly labelled "district".
    if "superintendent" in text:
        return "Dist"
    if "trustee" in text or "board member" in text or "school board" in text \
            or "board of education" in text:
        return "Dist"
    if re.search(r"\bdistrict\b", text):
        return "Dist"

    # 2) School-building leadership -------------------------------------
    if any(w in text for w in _LEAD_SCHOOL_LEADER_WORDS):
        return "School"

    # 3) Teachers / instructional --------------------------------------
    if employee_type == EmployeeType.TEACHER or \
            any(w in text for w in _LEAD_TEACHER_WORDS):
        return "Teacher"

    # 4) Ambiguous administrative titles -> resolve by scope ------------
    if any(w in text for w in _LEAD_ADMIN_WORDS):
        # Pure coaches are operational, not decision leads.
        if "coach" in text and "director" not in text:
            return "Other"
        return "Dist" if district_scope else "School"

    # 5) Everyone else (support, health, athletics, clerical, etc.) -----
    return "Other"


# ============================================================================
# HTTP LAYER  (retries, rate limiting, robots.txt, optional JS rendering)
# ============================================================================

class Fetcher:
    """Handles all network access politely and robustly."""

    def __init__(self, config: ScraperConfig):
        self.config = config
        self.session = self._build_session()
        self._last_request: Dict[str, float] = {}
        self._robots: Dict[str, Optional[RobotFileParser]] = {}
        self._lock = Lock()
        self._pw = None          # Playwright instance (lazy)
        self._browser = None     # Browser instance (lazy)

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        retry = Retry(
            total=self.config.max_retries,
            backoff_factor=self.config.retry_backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10,
                              pool_maxsize=20)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.verify = self.config.verify_ssl
        if not self.config.verify_ssl:
            try:
                import urllib3
                urllib3.disable_warnings()
            except Exception:
                pass
        return session

    # -- politeness ------------------------------------------------------
    def _respect_rate_limit(self, url: str) -> None:
        host = get_domain(url)
        with self._lock:
            last = self._last_request.get(host, 0.0)
            wait = self.config.rate_limit_delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.time()

    def _robots_allows(self, url: str) -> bool:
        if not self.config.respect_robots:
            return True
        host = get_domain(url)
        parsed = urlparse(url)
        if host not in self._robots:
            rp = RobotFileParser()
            robots_url = f"{parsed.scheme}://{host}/robots.txt"
            try:
                resp = self.session.get(robots_url, timeout=10)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None  # no robots => allow
            except Exception:
                rp = None
            self._robots[host] = rp
        rp = self._robots.get(host)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.config.user_agent, url)
        except Exception:
            return True

    # -- fetching --------------------------------------------------------
    def get(self, url: str) -> Optional[str]:
        """Static fetch with retries, rate limiting and robots awareness."""
        if not url or not url.startswith("http"):
            return None
        if not self._robots_allows(url):
            logger.debug("robots.txt disallows %s", url)
            return None
        self._respect_rate_limit(url)
        try:
            resp = self.session.get(url, timeout=self.config.request_timeout,
                                    allow_redirects=True)
            if resp.status_code >= 400:
                logger.debug("HTTP %s for %s", resp.status_code, url)
                return None
            ctype = resp.headers.get("Content-Type", "")
            if ctype and "html" not in ctype and "xml" not in ctype \
                    and "json" not in ctype:
                return None
            return resp.text
        except Exception as exc:
            logger.debug("fetch failed for %s: %s", url, exc)
            return None

    # -- optional JS rendering ------------------------------------------
    def get_rendered(self, url: str, wait_selector: Optional[str] = None
                     ) -> Optional[str]:
        """Fetch with a headless browser (Playwright). Falls back to static
        fetch if Playwright is unavailable."""
        if not self.config.use_javascript:
            return self.get(url)
        if not self._ensure_browser():
            return self.get(url)  # graceful fallback
        try:
            import asyncio
            return asyncio.get_event_loop().run_until_complete(
                self._render_async(url, wait_selector))
        except RuntimeError:
            # No running loop in this thread: make a fresh one.
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    self._render_async(url, wait_selector))
            finally:
                loop.close()
        except Exception as exc:
            logger.debug("JS render failed for %s: %s", url, exc)
            return self.get(url)

    def _ensure_browser(self) -> bool:
        if self._browser is not None:
            return True
        try:
            from playwright.async_api import async_playwright  # noqa
        except Exception:
            logger.warning("Playwright not installed; JS rendering disabled. "
                           "Install with: pip install playwright && "
                           "playwright install chromium")
            self.config.use_javascript = False
            return False
        return True  # actual browser launched lazily in _render_async

    async def _render_async(self, url: str, wait_selector: Optional[str]
                            ) -> Optional[str]:
        from playwright.async_api import async_playwright
        if self._pw is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await self._browser.new_context(
            user_agent=self.config.user_agent)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle",
                            timeout=self.config.request_timeout * 1000)
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=5000)
                except Exception:
                    pass
            await page.wait_for_timeout(self.config.js_wait_ms)
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
            return await page.content()
        finally:
            await context.close()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        if self._browser is not None:
            try:
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    self._close_browser_async())
            except Exception:
                pass

    async def _close_browser_async(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass


# ============================================================================
# CMS DETECTION
# ============================================================================

_CMS_SIGNATURES = {
    CMSType.FINALSITE: ("finalsite", "fs-cms", "fsconstituent", "fselement"),
    CMSType.BLACKBOARD: ("blackboard", "bbdn", "webcommunitymanager"),
    CMSType.EDLIO: ("edlio", "edl.io"),
    CMSType.APPTEGY: ("apptegy", "thrillshare"),
    CMSType.SCHOOLBLOCKS: ("schoolblocks",),
    CMSType.GABBART: ("gabbart",),
    CMSType.PARENTSQUARE: ("parentsquare",),
    CMSType.SQUARESPACE: ("squarespace",),
    CMSType.WORDPRESS: ("wp-content", "wp-includes"),
}


def detect_cms(html: str, url: str) -> CMSType:
    hay = (html or "").lower() + " " + (url or "").lower()
    for cms, sigs in _CMS_SIGNATURES.items():
        if any(sig in hay for sig in sigs):
            return cms
    return CMSType.UNKNOWN


# ============================================================================
# SCHOOL DISCOVERY
# ============================================================================

_SCHOOL_URL_PATTERNS = [
    r"/schools?/", r"/campus(?:es)?/", r"/sites?/", r"/locations?/",
    r"-elementary", r"-middle", r"-high-?school", r"-academy",
]
_SKIP_LINK_WORDS = (
    "home", "login", "sign in", "contact", "calendar", "news", "event",
    "search", "policy", "menu", "nav", "back to", "privacy", "sitemap",
    "translate", "facebook", "twitter", "instagram", "youtube",
)
_SKIP_EXTENSIONS = (".pdf", ".doc", ".docx", ".zip", ".jpg", ".jpeg", ".png",
                    ".gif", ".mp4", ".xls", ".xlsx")


def _same_site(candidate: str, base: str) -> bool:
    cd, bd = get_domain(candidate), get_domain(base)
    if not cd or not bd:
        return False
    return cd == bd or cd.endswith("." + bd) or bd.endswith("." + cd) \
        or get_root_domain(candidate) == get_root_domain(base)


def discover_schools(fetcher: Fetcher, district_url: str,
                     district_name: str) -> List[School]:
    """Find schools in a district via homepage links + sitemap.xml.
    Generalized: no hardcoded subdomains."""
    logger.info("Discovering schools for %s ...", district_name)
    found: Dict[str, str] = {}   # url -> name
    base = district_url

    html = fetcher.get(district_url)
    if html:
        soup = BeautifulSoup(html, "lxml")
        for link in soup.find_all("a", href=True):
            text = clean_text(link.get_text())
            href = link["href"]
            if not text or len(text) < 3 or len(text) > 100:
                continue
            tl, hl = text.lower(), href.lower()
            if any(w in tl for w in _SKIP_LINK_WORDS):
                continue
            is_school = any(w in tl for w in _SCHOOL_WORDS) or \
                any(re.search(p, hl) for p in _SCHOOL_URL_PATTERNS)
            if not is_school:
                continue
            abs_url = urljoin(district_url, href)
            if any(abs_url.lower().endswith(e) for e in _SKIP_EXTENSIONS):
                continue
            if not _same_site(abs_url, base):
                continue
            # Prefer bare site root for subdomain schools.
            p = urlparse(abs_url)
            key = abs_url if p.path not in ("", "/") else f"{p.scheme}://{p.netloc}"
            found.setdefault(key, text.strip())

    # sitemap.xml (handles sitemap indexes one level deep)
    try:
        _harvest_sitemap(fetcher, urljoin(district_url, "/sitemap.xml"),
                         base, found, depth=0)
    except Exception as exc:
        logger.debug("sitemap parse failed: %s", exc)

    schools = [School(name=name, url=url, district=district_name,
                      district_url=district_url)
               for url, name in found.items()]
    logger.info("  -> found %d schools", len(schools))
    return schools


def _harvest_sitemap(fetcher: Fetcher, sitemap_url: str, base: str,
                     found: Dict[str, str], depth: int) -> None:
    if depth > 1:
        return
    xml = fetcher.get(sitemap_url)
    if not xml:
        return
    try:
        root = ET.fromstring(xml)
    except Exception:
        return
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    # nested sitemaps
    for sm in root.findall(".//sm:sitemap/sm:loc", ns):
        if sm.text:
            _harvest_sitemap(fetcher, sm.text.strip(), base, found, depth + 1)
    for loc in root.findall(".//sm:url/sm:loc", ns):
        url = (loc.text or "").strip()
        if not url or not _same_site(url, base):
            continue
        low = url.lower()
        if any(re.search(p, low) for p in _SCHOOL_URL_PATTERNS):
            segs = [s for s in urlparse(url).path.split("/") if s]
            if segs:
                name = segs[-1].replace("-", " ").replace("_", " ").title()
                if len(name) > 3:
                    found.setdefault(url, name)


# ============================================================================
# DIRECTORY DISCOVERY
# ============================================================================

_DIRECTORY_KEYWORDS = (
    "directory", "staff", "faculty", "employee", "team", "administration",
    "leadership", "personnel", "people", "our-team", "our-staff",
    "meet-the", "meet-our", "contact-us", "who-we-are",
)


def is_directory_page(soup: BeautifulSoup) -> bool:
    """Does this page look like it lists multiple staff members?"""
    mailto = soup.find_all("a", href=re.compile(r"^mailto:", re.I))
    if len(mailto) >= 3:
        return True
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) >= 4 and any(
                r.find("a", href=re.compile(r"^mailto:", re.I)) for r in rows):
            return True
    cards = soup.find_all(
        class_=re.compile(r"(staff|employee|person|faculty|member|profile)", re.I))
    if len(cards) >= 3:
        return True
    return False


def find_directories(fetcher: Fetcher, school: School,
                     config: ScraperConfig) -> List[str]:
    """Find candidate staff-directory URLs for a school (shallow crawl)."""
    directories: List[str] = []
    seen_dir = set()
    visited = set()

    def add(url: str) -> None:
        if url not in seen_dir:
            seen_dir.add(url)
            directories.append(url)

    def crawl(url: str, depth: int) -> None:
        if depth > config.directory_crawl_depth or url in visited:
            return
        if len(directories) >= config.max_directories_per_school:
            return
        visited.add(url)
        html = fetcher.get(url)
        if not html:
            return
        soup = BeautifulSoup(html, "lxml")
        if is_directory_page(soup):
            add(url)
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue
            text = clean_text(link.get_text()).lower()
            hl = href.lower()
            if any(k in text for k in _DIRECTORY_KEYWORDS) or \
                    any(k in hl for k in _DIRECTORY_KEYWORDS):
                abs_url = urljoin(url, href)
                if any(abs_url.lower().endswith(e) for e in _SKIP_EXTENSIONS):
                    continue
                if _same_site(abs_url, school.url):
                    add(abs_url)
                    if len(directories) < config.max_directories_per_school:
                        crawl(abs_url, depth + 1)

    crawl(school.url, 0)
    return directories[: config.max_directories_per_school]


# ============================================================================
# PARSING STRATEGIES
# ============================================================================

def create_employee(name, title, email, phone, dept, school, directory_url,
                     photo_url=None, profile_url=None) -> Optional[Employee]:
    """Build an Employee, fixing swapped name/title and inferring role."""
    name = clean_email_prefix(name)
    title = clean_email_prefix(title)

    # Fix swapped name/title (title holds the person, name holds a label).
    if title and name and looks_like_person(title) and not looks_like_person(name):
        name, title = title, "Staff"

    if not name or len(clean_text(name)) < 3:
        return None
    if is_junk_name(name):
        return None
    if not looks_like_person(name) and "@" not in (email or ""):
        # Not clearly a person and no email to anchor on -> skip junk.
        # (Single-word names with an email are still allowed through.)
        if len(clean_text(name).split()) < 2:
            return None

    first, last = normalize_name(name)
    role, department, emp_type = infer_role(
        name, title, email, directory_url, school.name)

    final_title = clean_text(title)
    if (not final_title or final_title.lower() in ("staff", "nan", "none")
            or looks_like_person(final_title)):
        final_title = role

    phone_fmt, ext = normalize_phone(phone)
    lead = classify_lead(final_title, role, emp_type,
                         school.district or "", school.name)

    return Employee(
        district=school.district or "",
        school=school.name,
        name=clean_text(name),
        first_name=first,
        last_name=last,
        title=final_title,
        department=clean_text(dept) or department,
        email=normalize_email(email),
        phone=phone_fmt,
        extension=ext,
        photo_url=photo_url,
        profile_url=profile_url,
        school_url=school.url,
        directory_url=directory_url,
        employee_type=emp_type,
        lead_type=lead,
    )


def parse_finalsite(soup, school, directory_url) -> List[Employee]:
    employees = []
    items = soup.select(".fsConstituentItem, .fsConstituentProfile")
    for item in items:
        name_el = item.select_one(".fsFullName, .fsConstituentName, "
                                  ".fsFullNameLink, h3, h4, .name")
        if not name_el:
            continue
        name = clean_text(name_el.get_text())
        title_el = item.select_one(".fsTitles, .fsConstituentTitle, "
                                   ".title, .position")
        title = clean_text(title_el.get_text()) if title_el else None
        email = None
        mail = item.find("a", href=re.compile(r"^mailto:", re.I))
        if mail:
            email = mail["href"]
        dept_el = item.select_one(".fsDepartments, .department")
        dept = clean_text(dept_el.get_text()) if dept_el else None
        img = item.find("img")
        photo = urljoin(directory_url, img["src"]) if img and img.get("src") else None
        emp = create_employee(name, title, email, None, dept, school,
                              directory_url, photo)
        if emp:
            employees.append(emp)
    return employees


def find_employees_in_data(data: Any) -> List[Dict]:
    """Recursively locate employee-like dict lists inside arbitrary JSON."""
    results: List[Dict] = []
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            keys = {k.lower() for k in data[0].keys()}
            if keys & {"name", "email", "title", "firstname", "fullname",
                       "first_name", "displayname", "display_name",
                       "staffname", "employeename", "position", "jobtitle",
                       "job_title", "role", "emailaddress", "last_name",
                       "lastname"}:
                return list(data)
        for item in data:
            results.extend(find_employees_in_data(item))
    elif isinstance(data, dict):
        for value in data.values():
            results.extend(find_employees_in_data(value))
    return results


def create_employee_from_dict(data: Dict, school, directory_url
                              ) -> Optional[Employee]:
    def pick(*keys):
        for k in keys:
            for actual in data:
                if actual.lower() == k:
                    v = data[actual]
                    if v:
                        return v
        return None

    name = pick("name", "fullname", "full_name", "displayname",
                "display_name", "staffname", "employeename")
    if not name:
        first = pick("firstname", "first_name", "first", "givenname")
        last = pick("lastname", "last_name", "last", "familyname", "surname")
        name = " ".join([p for p in (first, last) if p]) or None
    if not name:
        return None
    title = pick("title", "position", "role", "jobtitle", "job_title",
                 "positiontitle")
    email = pick("email", "emailaddress", "email_address", "mail")
    phone = pick("phone", "phonenumber", "phone_number", "telephone", "tel")
    dept = pick("department", "dept", "division")
    photo = pick("photo", "photourl", "photo_url", "image", "imageurl",
                 "imageurl", "headshot", "avatar")
    return create_employee(str(name), str(title) if title else None,
                           str(email) if email else None,
                           str(phone) if phone else None,
                           str(dept) if dept else None,
                           school, directory_url,
                           str(photo) if photo else None)


def parse_embedded_json(soup, school, directory_url) -> List[Employee]:
    employees = []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or "{" not in raw:
            continue
        stype = (script.get("type") or "").lower()
        candidates = []
        if stype in ("application/json", "application/ld+json"):
            candidates.append(raw)
        else:
            for pat in (
                r"(?:var|let|const|window\.\w+)\s*(?:employees|staff|directory|"
                r"people|persons|members)\s*=\s*(\[.*?\]);",
                r"\"(?:employees|staff|persons|people|members)\"\s*:\s*(\[.*?\])",
            ):
                m = re.search(pat, raw, re.DOTALL | re.IGNORECASE)
                if m:
                    candidates.append(m.group(1))
        for cand in candidates:
            try:
                data = json.loads(cand)
            except Exception:
                continue
            for ed in find_employees_in_data(data):
                emp = create_employee_from_dict(ed, school, directory_url)
                if emp:
                    employees.append(emp)
    return employees


def parse_table_row(row, school, directory_url) -> Optional[Employee]:
    cells = row.find_all(["td", "th"])
    if len(cells) < 2:
        return None
    name = title = email = phone = dept = None
    for i, cell in enumerate(cells):
        text = clean_text(cell.get_text())
        mail = cell.find("a", href=re.compile(r"^mailto:", re.I))
        if mail and not email:
            email = mail["href"]
        pm = _PHONE_IN_TEXT_RE.search(text)
        if pm and not phone and re.fullmatch(r"[\d\s().\-+]+", text):
            phone = pm.group(0)
            continue
        if i == 0 and text and not name:
            name = text
        elif i == 1 and text and not title:
            title = text
        elif not dept and text and i > 1 and not any(c.isdigit() for c in text[:4]):
            dept = text
    return create_employee(name, title, email, phone, dept, school,
                           directory_url)


def parse_card(elem, school, directory_url) -> Optional[Employee]:
    name = None
    for sel in ("h2", "h3", "h4", "h5", ".name", '[class*="name"]', "strong"):
        try:
            n = elem.select_one(sel)
        except Exception:
            continue
        if n:
            t = clean_text(n.get_text())
            if t and 2 < len(t) < 80 and not any(
                    w in t.lower() for w in ("click", "more", "contact")):
                name = t
                break
    if not name:
        return None
    title = None
    for sel in (".title", ".position", ".role", '[class*="title"]',
                '[class*="position"]', "p", "span"):
        try:
            el = elem.select_one(sel)
        except Exception:
            continue
        if el:
            t = clean_text(el.get_text())
            if t and t != name and 2 < len(t) < 120:
                title = t
                break
    email = None
    mail = elem.find("a", href=re.compile(r"^mailto:", re.I))
    if mail:
        email = mail["href"]
    if not email:
        # obfuscated / data-attribute emails within this card
        cf = elem.select_one("[data-cfemail]")
        if cf:
            email = _decode_cloudflare(cf.get("data-cfemail", ""))
    if not email:
        da = elem.select_one("[data-email], [data-mail]")
        if da:
            email = da.get("data-email") or da.get("data-mail")
    if not email:
        om = _OBFUSCATED_EMAIL_RE.search(elem.get_text(" "))
        if om:
            email = f"{om.group(1)}@{om.group(2)}.{om.group(3)}"
    phone = None
    pm = _PHONE_IN_TEXT_RE.search(elem.get_text())
    if pm:
        phone = pm.group(0)
    dept = None
    for sel in (".department", '[class*="department"]', ".dept"):
        try:
            d = elem.select_one(sel)
        except Exception:
            continue
        if d:
            dept = clean_text(d.get_text())
            break
    img = elem.find("img")
    photo = urljoin(directory_url, img["src"]) if img and img.get("src") else None
    profile = None
    a = elem.find("a", href=True)
    if a and not a["href"].lower().startswith("mailto:"):
        profile = urljoin(directory_url, a["href"])
    return create_employee(name, title, email, phone, dept, school,
                           directory_url, photo, profile)


def parse_by_mailto(soup, school, directory_url) -> List[Employee]:
    employees = []
    for link in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        email = link["href"]
        name = clean_text(link.get_text())
        title = None
        _label = name.lower().strip(" :>-")
        if (not name or "@" in name or len(name) < 3
                or _contains_role_word(name)
                or _label in ("email", "e-mail", "mail", "contact",
                              "click here", "send email", "send", "more",
                              "read more", "view profile", "profile")):
            name = None
            # Walk up a few ancestors looking for a heading = the person's name.
            node = link
            for _ in range(3):
                node = node.parent
                if node is None:
                    break
                heading = node.find(["h1", "h2", "h3", "h4", "h5", "h6"])
                if heading:
                    htext = clean_text(heading.get_text())
                    if htext and 2 < len(htext) < 60 and not _contains_role_word(htext):
                        name = htext
                        for sib in (heading.find_next_sibling(),):
                            if sib:
                                st = clean_text(sib.get_text())
                                if st and st != htext and len(st) < 80:
                                    title = st
                        break
            # No heading: fall back to the immediate parent's text, minus the
            # email address itself (covers "Iris Chen  ichen@x.edu" list items).
            if not name:
                parent = link.parent
                if parent:
                    ptext = clean_text(parent.get_text())
                    addr = normalize_email(email) or ""
                    ptext = clean_text(ptext.replace(addr, "")
                                       .replace(clean_text(link.get_text()), ""))
                    if (2 < len(ptext) < 60 and not _contains_role_word(ptext)):
                        name = ptext
        if not name or "@" in name or len(name) < 3 or is_junk_name(name):
            continue
        emp = create_employee(name, title, email, None, None, school,
                              directory_url)
        if emp:
            employees.append(emp)
    return employees


def parse_microdata(soup, school, directory_url) -> List[Employee]:
    """schema.org Person microdata: <div itemscope itemtype=.../Person>."""
    employees = []
    for scope in soup.select('[itemscope][itemtype*="Person" i]'):
        def prop(name):
            el = scope.find(attrs={"itemprop": name})
            if not el:
                return None
            return el.get("content") or clean_text(el.get_text())
        name = prop("name")
        if not name:
            name = " ".join(filter(None, [prop("givenName"),
                                          prop("familyName")])) or None
        title = prop("jobTitle") or prop("title")
        email = prop("email")
        phone = prop("telephone")
        emp = create_employee(name, title, email, phone, prop("department"),
                              school, directory_url)
        if emp:
            employees.append(emp)
    return employees


def parse_hcard(soup, school, directory_url) -> List[Employee]:
    """hCard / h-card microformats (.vcard .fn .email .title)."""
    employees = []
    for card in soup.select(".vcard, .h-card"):
        n = card.select_one(".fn, .p-name")
        if not n:
            continue
        name = clean_text(n.get_text())
        t = card.select_one(".title, .role, .p-job-title, .org")
        title = clean_text(t.get_text()) if t else None
        email = None
        em = card.select_one(".email, .u-email")
        if em:
            a = em.find("a", href=re.compile(r"^mailto:", re.I))
            email = a["href"] if a else em.get_text()
        tel = card.select_one(".tel, .p-tel")
        phone = clean_text(tel.get_text()) if tel else None
        emp = create_employee(name, title, email, phone, None, school,
                              directory_url)
        if emp:
            employees.append(emp)
    return employees


def parse_definition_lists(soup, school, directory_url) -> List[Employee]:
    """<dl><dt>Name</dt><dd>Title / contact</dd> style directories."""
    employees = []
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        if len(dts) < 2 or len(dts) != len(dds):
            continue
        for dt, dd in zip(dts, dds):
            name = clean_text(dt.get_text())
            mail = (dd.find("a", href=re.compile(r"^mailto:", re.I))
                    or dt.find("a", href=re.compile(r"^mailto:", re.I)))
            email = mail["href"] if mail else None
            if mail:
                mail.extract()  # drop link text so it doesn't pollute title
            title = clean_text(dd.get_text())
            emp = create_employee(name, title, email, None, None, school,
                                  directory_url)
            if emp:
                employees.append(emp)
    return employees


_CARD_SELECTORS = [
    'div[class*="staff"]', 'div[class*="employee"]', 'div[class*="faculty"]',
    'div[class*="person"]', 'div[class*="member"]', 'div[class*="profile"]',
    'div[class*="directory-item"]', 'div[class*="teammember"]',
    'div[class*="team-member"]', 'div[class*="bio"]', 'div[class*="contact-card"]',
    'article[class*="staff"]', 'article[class*="person"]',
    'li[class*="staff"]', 'li[class*="member"]', 'li[class*="person"]',
    'li[class*="employee"]', "[data-employee]", "[data-staff]",
    ".ce_staff", ".staff-list li", ".directory li",
]


def _strip_noise_elements(soup: "BeautifulSoup") -> None:
    """Remove page chrome that commonly pollutes staff parsing: script/style,
    and especially <select> country/language dropdowns, plus nav/footer menus.
    Mutates the soup in place."""
    for tag in soup.find_all(["script", "style", "noscript", "select",
                              "option", "datalist", "nav", "footer"]):
        try:
            tag.decompose()
        except Exception:
            pass


def parse_soup(soup: "BeautifulSoup", school: School,
               directory_url: str) -> List[Employee]:
    """Run all parsing strategies in priority order on a parsed page.

    Order matters: precise/structured sources first, loose text last.
    """
    # 0) Extract embedded JSON BEFORE stripping (JSON lives in <script>).
    json_emps = parse_embedded_json(soup, school, directory_url)

    # Remove menus/dropdowns/footers so they aren't mistaken for people.
    _strip_noise_elements(soup)

    # 1) Platform-specific (Finalsite)
    if soup.select_one(".fsConstituentItem, .fsConstituentProfile, .fsDirectory"):
        emps = parse_finalsite(soup, school, directory_url)
        if emps:
            return emps

    # 2) Embedded JSON / JSON-LD / __NEXT_DATA__ / inline arrays
    if json_emps:
        return json_emps

    # 3) schema.org Person microdata
    emps = parse_microdata(soup, school, directory_url)
    if len(emps) >= 2:
        return emps

    # 4) hCard / vCard microformats
    emps = parse_hcard(soup, school, directory_url)
    if len(emps) >= 2:
        return emps

    # 5) Tables
    table_emps: List[Employee] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        for row in rows[1:]:
            emp = parse_table_row(row, school, directory_url)
            if emp:
                table_emps.append(emp)
    if table_emps:
        return table_emps

    # 6) Cards / list items. Specific "staff/employee/faculty" selectors are
    #    trusted even with a single match (small district admin pages have one
    #    card); generic selectors still need >=2 to avoid false positives.
    _SPECIFIC = ('staff', 'employee', 'faculty', 'teammember', 'team-member',
                 'person')
    for selector in _CARD_SELECTORS:
        try:
            elements = soup.select(selector)
        except Exception:
            continue
        if not elements:
            continue
        min_needed = 1 if any(k in selector for k in _SPECIFIC) else 2
        if len(elements) >= min_needed:
            card_emps = [e for e in
                         (parse_card(el, school, directory_url) for el in elements)
                         if e]
            if len(card_emps) >= min_needed:
                return card_emps

    # 7) Definition lists
    emps = parse_definition_lists(soup, school, directory_url)
    if len(emps) >= 2:
        return emps

    # 8) Mailto fallback (last resort)
    return parse_by_mailto(soup, school, directory_url)


def parse_html(html: str, school: School, directory_url: str) -> List[Employee]:
    """Parse a page's raw HTML with all strategies (convenience wrapper)."""
    return parse_soup(BeautifulSoup(html, "lxml"), school, directory_url)


# ============================================================================
# PAGINATION
# ============================================================================

_NEXT_TEXT = {"next", "next page", "next »", "»", "›", ">", "more",
              "load more", "show more", "view more"}


def find_next_page(soup: "BeautifulSoup", base_url: str) -> Optional[str]:
    """Best-effort detection of a 'next page' link for paginated directories."""
    link = soup.find("a", rel="next") or soup.find("link", rel="next")
    if link and link.get("href"):
        return urljoin(base_url, link["href"])
    containers = soup.select(
        '.pagination a, .pager a, [class*="pagination"] a, [class*="paging"] a, '
        'nav[aria-label*="pag" i] a')
    for a in containers:
        href = a.get("href")
        if not href or href.startswith("#") or "javascript" in href.lower():
            continue
        label = (clean_text(a.get_text()).lower()
                 + " " + (a.get("aria-label") or "").lower())
        if any(tok == label.strip() or tok in label.split()
               for tok in _NEXT_TEXT):
            return urljoin(base_url, href)
    return None


def scrape_one_directory(fetcher: "Fetcher", url: str, school: School,
                         config: ScraperConfig, max_pages: int = 25
                         ) -> List[Employee]:
    """Fetch and parse a directory URL, following pagination up to max_pages."""
    employees: List[Employee] = []
    visited = set()
    current = url
    pages = 0
    wait = '[class*="staff"], [class*="directory"], [class*="member"], table'
    while current and current not in visited and pages < max_pages:
        visited.add(current)
        pages += 1
        html = fetcher.get_rendered(current, wait_selector=wait) \
            if config.use_javascript else fetcher.get(current)
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        employees.extend(parse_soup(soup, school, current))
        nxt = find_next_page(soup, current)
        if not nxt or nxt in visited:
            break
        current = nxt
    return employees


# ============================================================================
# EMAIL EXTRACTION & ENRICHMENT
# ============================================================================
# Many directories hide emails: on a per-person profile page, behind Cloudflare
# obfuscation, or written as "name (at) district dot org". These helpers dig
# those out.

_OBFUSCATED_EMAIL_RE = re.compile(
    r"([a-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\s+at\s+|@)\s*"
    r"([a-z0-9.\-]+?)\s*(?:\[dot\]|\(dot\)|\s+dot\s+|\.)\s*"
    r"([a-z]{2,})", re.IGNORECASE)


def _decode_cloudflare(hex_str: str) -> Optional[str]:
    """Decode a Cloudflare data-cfemail obfuscated address."""
    try:
        key = int(hex_str[:2], 16)
        out = "".join(chr(int(hex_str[i:i + 2], 16) ^ key)
                      for i in range(2, len(hex_str), 2))
        return normalize_email(out)
    except Exception:
        return None


def extract_emails_from_soup(soup: "BeautifulSoup") -> List[str]:
    """Pull every email from a page: mailto, data attributes, Cloudflare
    obfuscation, and 'name at domain dot com' text."""
    found: List[str] = []

    def add(e):
        e = normalize_email(e)
        if e and e not in found:
            found.append(e)

    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        add(a["href"])
    for el in soup.select("[data-email], [data-mail], [data-cfemail]"):
        if el.get("data-cfemail"):
            add(_decode_cloudflare(el["data-cfemail"]))
        add(el.get("data-email") or el.get("data-mail"))
    for a in soup.select("a.__cf_email__[data-cfemail]"):
        add(_decode_cloudflare(a["data-cfemail"]))
    # Obfuscated text like "jdoe (at) district dot org"
    text = soup.get_text(" ")
    for m in _OBFUSCATED_EMAIL_RE.finditer(text):
        add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    return found


def enrich_emails(fetcher: "Fetcher", employees: List[Employee],
                  config: ScraperConfig, max_profiles: int = 400) -> None:
    """For employees that have a profile_url but no email, open the profile
    page and try to extract the email. Mutates employees in place."""
    fetched = 0
    for emp in employees:
        if emp.email or not emp.profile_url:
            continue
        if fetched >= max_profiles:
            break
        html = (fetcher.get_rendered(emp.profile_url)
                if config.use_javascript else fetcher.get(emp.profile_url))
        fetched += 1
        if not html:
            continue
        try:
            emails = extract_emails_from_soup(BeautifulSoup(html, "lxml"))
        except Exception:
            emails = []
        if emails:
            # Prefer an address whose local part matches the person's name.
            last = (emp.last_name or "").lower()
            first = (emp.first_name or "").lower()
            best = None
            for e in emails:
                local = e.split("@")[0].lower()
                if (last and last in local) or (first and first in local):
                    best = e
                    break
            emp.email = best or emails[0]


# ============================================================================
# DISTRICT FAN-OUT  (district homepage -> campuses -> each staff directory)
# ============================================================================

def scrape_district_full(fetcher: "Fetcher", district_name: str, website: str,
                         known_directory_url: Optional[str],
                         config: ScraperConfig, max_schools: int = 25,
                         follow_profiles: bool = True) -> List[Employee]:
    """Scrape a whole district: the district-level directory PLUS every campus
    it can discover. Returns deduplicated Employees, each tagged with its real
    school name. Individual site failures are contained (won't abort the run).
    """
    leads: List[Employee] = []
    seen_dirs: set = set()

    # Targets = (school_name, school_url, optional_known_directory_url)
    targets: List[Tuple[str, str, Optional[str]]] = [
        (district_name, website, known_directory_url)]
    try:
        schools = discover_schools(fetcher, website, district_name)
    except Exception as exc:
        logger.debug("discover_schools failed for %s: %s", district_name, exc)
        schools = []
    for sc in schools[:max_schools]:
        # Skip a discovered "campus" that is just the district site again.
        if get_domain(sc.url) == get_domain(website) and sc.name == district_name:
            continue
        targets.append((sc.name, sc.url, None))

    for name, url, dir_url in targets:
        school = School(name=name, url=url, district=district_name)
        try:
            directory_urls = [dir_url] if dir_url else \
                (find_directories(fetcher, school, config) or [url])
        except Exception as exc:
            logger.debug("find_directories failed for %s: %s", name, exc)
            directory_urls = [url]
        for du in directory_urls:
            if not du or du in seen_dirs:
                continue
            seen_dirs.add(du)
            try:
                people = scrape_one_directory(fetcher, du, school, config)
                if follow_profiles:
                    enrich_emails(fetcher, people, config)
                leads.extend(people)
            except Exception as exc:
                logger.debug("scrape failed for %s: %s", du, exc)

    return deduplicate(leads)


# ============================================================================

def deduplicate(employees: List[Employee]) -> List[Employee]:
    seen = set()
    unique = []
    for emp in employees:
        if emp.email:
            key = f"{emp.district}|{emp.email.lower()}"
        else:
            key = (f"{emp.district}|{emp.school}|"
                   f"{str(emp.name).lower()}|{str(emp.title).lower()}")
        if key not in seen:
            seen.add(key)
            unique.append(emp)
    return unique


# ============================================================================
# EXPORT
# ============================================================================
# Output schema (exactly as requested):
#   district | school | first_name | last_name | email | role | lead_type
# where lead_type is one of Dist / School / Teacher / Other.

LEAD_TYPE_ORDER = ["Dist", "School", "Teacher", "Other"]

OUTPUT_COLUMNS = [
    "district", "school", "first_name", "last_name", "email", "role",
    "lead_type",
]

# Friendly headers written to the files.
OUTPUT_HEADERS = {
    "district": "District Name",
    "school": "School Name",
    "first_name": "First Name",
    "last_name": "Last Name",
    "email": "Email",
    "role": "Role",
    "lead_type": "Lead Type",
}


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_") or "output"


def _lead_rows(employees: List[Employee]) -> List[Dict[str, Any]]:
    """Project Employee objects onto the requested output schema."""
    rows = []
    for e in employees:
        rows.append({
            "district": e.district,
            "school": e.school,
            "first_name": e.first_name,
            "last_name": e.last_name,
            "email": e.email,
            "role": e.title,          # cleaned / inferred role string
            "lead_type": e.lead_type,
        })
    return rows


def export_results(employees: List[Employee], schools: List[School],
                   district_name: str, output_dir: str) -> Path:
    dist_dir = Path(output_dir) / _safe_name(district_name)
    dist_dir.mkdir(parents=True, exist_ok=True)

    if not employees:
        logger.warning("No leads to export")
        return dist_dir

    rows = _lead_rows(employees)

    # --- CSV fallback path (no pandas) --------------------------------
    if not _HAS_PANDAS:
        import csv

        def write_csv(path, subset):
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow([OUTPUT_HEADERS[c] for c in OUTPUT_COLUMNS])
                for r in subset:
                    w.writerow([r[c] if r[c] is not None else "" for c in OUTPUT_COLUMNS])

        write_csv(dist_dir / "all_leads.csv", rows)
        for lt in LEAD_TYPE_ORDER:
            subset = [r for r in rows if r["lead_type"] == lt]
            if subset:
                write_csv(dist_dir / f"leads_{lt.lower()}.csv", subset)
        logger.info("  wrote all_leads.csv (%d rows, pandas unavailable)",
                    len(rows))
        return dist_dir

    # --- pandas path ---------------------------------------------------
    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    df = df.sort_values(["lead_type", "school", "last_name", "first_name"],
                        kind="stable")
    out_df = df.rename(columns=OUTPUT_HEADERS)

    out_df.to_csv(dist_dir / "all_leads.csv", index=False,
                  encoding="utf-8-sig")
    logger.info("  wrote all_leads.csv (%d rows)", len(out_df))

    for lt in LEAD_TYPE_ORDER:
        subset = out_df[df["lead_type"].values == lt]
        if len(subset):
            subset.to_csv(dist_dir / f"leads_{lt.lower()}.csv", index=False,
                          encoding="utf-8-sig")

    try:
        xlsx = dist_dir / "leads_master.xlsx"
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            counts = df["lead_type"].value_counts()
            summary = [
                {"Metric": "Total Leads", "Count": len(df)},
                {"Metric": "Schools", "Count": len(schools)},
                {"Metric": "With Email",
                 "Count": int(df["email"].notna().sum())},
            ]
            for lt in LEAD_TYPE_ORDER:
                summary.append({"Metric": f"Lead: {lt}",
                                "Count": int(counts.get(lt, 0))})
            pd.DataFrame(summary).to_excel(writer, sheet_name="Summary",
                                           index=False)
            out_df.to_excel(writer, sheet_name="All Leads", index=False)
            for lt in LEAD_TYPE_ORDER:
                subset = out_df[df["lead_type"].values == lt]
                if len(subset):
                    subset.to_excel(writer, sheet_name=lt[:31], index=False)
            pd.DataFrame([{
                "School": s.name, "URL": s.url,
                "Directory URLs": "; ".join(s.directory_urls),
                "Leads": s.employee_count, "Error": s.error,
            } for s in schools]).to_excel(writer, sheet_name="Schools",
                                          index=False)
        logger.info("  wrote leads_master.xlsx")
    except Exception as exc:
        logger.warning("Could not write XLSX (%s); CSVs still available", exc)

    return dist_dir


# ============================================================================
# ORCHESTRATION
# ============================================================================

def _print_header(title: str, **rows: Any) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    for k, v in rows.items():
        print(f"{k+':':<18} {v}")
    print("=" * 70 + "\n")


def scrape_directory_page(url: str, school_name: str = "School",
                          district_name: str = "",
                          config: Optional[ScraperConfig] = None) -> Dict:
    """Scrape a single staff-directory URL."""
    config = config or ScraperConfig()
    setup_logging()
    fetcher = Fetcher(config)
    try:
        school = School(name=school_name, url=url, district=district_name)
        employees = deduplicate(
            scrape_one_directory(fetcher, url, school, config))
        school.employee_count = len(employees)
        school.directory_urls = [url]
        out = export_results(employees, [school], district_name or school_name,
                             config.output_dir)
        return {"success": True, "employees": employees, "schools": [school],
                "output_dir": str(out)}
    finally:
        fetcher.close()


def scrape_district(district_url: str, district_name: Optional[str] = None,
                    config: Optional[ScraperConfig] = None) -> Dict:
    """Discover schools in a district and scrape each one's staff directory."""
    config = config or ScraperConfig()
    setup_logging()

    if not district_name:
        district_name = get_domain(district_url).replace("www.", "").split(".")[0].title()

    start = time.time()
    _print_header("MASTER STAFF DIRECTORY SCRAPER",
                  District=district_name, URL=district_url,
                  JS_rendering=config.use_javascript)

    fetcher = Fetcher(config)
    try:
        # 1) Detect CMS
        home = fetcher.get(district_url)
        if home is None:
            logger.error("Could not access %s", district_url)
            return {"success": False, "error": "district URL unreachable"}
        cms = detect_cms(home, district_url)
        logger.info("CMS detected: %s", cms.value)

        # 2) Discover schools
        schools = discover_schools(fetcher, district_url, district_name)
        if not schools:
            logger.warning("No schools discovered; treating district URL as a "
                           "single directory.")
            schools = [School(name=district_name, url=district_url,
                              district=district_name, district_url=district_url)]
        if config.max_schools > 0:
            schools = schools[: config.max_schools]
        for s in schools:
            s.cms_type = cms

        # 3) For each school, find + scrape directories
        all_emps: List[Employee] = []
        try:
            from tqdm import tqdm
            iterator = tqdm(schools, desc="Schools", unit="school")
        except Exception:
            iterator = schools

        for school in iterator:
            try:
                dirs = find_directories(fetcher, school, config) or [school.url]
                school.directory_urls = dirs
                school_emps: List[Employee] = []
                for durl in dirs:
                    school_emps.extend(
                        scrape_one_directory(fetcher, durl, school, config))
                school_emps = deduplicate(school_emps)
                school.employee_count = len(school_emps)
                all_emps.extend(school_emps)
                if school_emps:
                    logger.info("  %s: %d employees", school.name,
                                len(school_emps))
            except Exception as exc:
                school.error = str(exc)
                logger.error("  error with %s: %s", school.name, exc)

        # 4) Deduplicate globally + export
        unique = deduplicate(all_emps)
        out = export_results(unique, schools, district_name, config.output_dir)
        duration = time.time() - start

        lead_counts = {lt: sum(1 for e in unique if e.lead_type == lt)
                       for lt in LEAD_TYPE_ORDER}
        _print_header(
            "SCRAPING COMPLETE",
            District=district_name, CMS=cms.value, Schools=len(schools),
            Total_Leads=len(unique),
            Leads=" ".join(f"{lt}={lead_counts[lt]}" for lt in LEAD_TYPE_ORDER),
            With_Email=sum(1 for e in unique if e.email),
            Duration=f"{duration:.1f}s", Output=out)

        return {"success": True, "district": district_name, "cms": cms.value,
                "schools": schools, "employees": unique,
                "duration": duration, "output_dir": str(out)}
    finally:
        fetcher.close()


# ============================================================================
# ANALYSIS HELPERS
# ============================================================================

def get_dataframe(result: Dict):
    if not _HAS_PANDAS:
        raise RuntimeError("pandas is not installed")
    emps = result.get("employees") or []
    return pd.DataFrame([e.to_dict() for e in emps])


def show_stats(result: Dict) -> None:
    emps = result.get("employees") or []
    if not emps:
        print("No employees found.")
        return
    total = len(emps)
    print("\n" + "=" * 70)
    print(f"STATISTICS  ({total} leads)")
    print("=" * 70)
    by_lead: Dict[str, int] = {}
    by_role: Dict[str, int] = {}
    by_school: Dict[str, int] = {}
    with_email = 0
    for e in emps:
        by_lead[e.lead_type] = by_lead.get(e.lead_type, 0) + 1
        by_role[e.title] = by_role.get(e.title, 0) + 1
        by_school[e.school] = by_school.get(e.school, 0) + 1
        with_email += 1 if e.email else 0
    print("\nBy lead type:")
    for lt in LEAD_TYPE_ORDER:
        c = by_lead.get(lt, 0)
        print(f"  {lt:<8} {c:>5}  ({c/total*100:.1f}%)")
    print(f"\nWith email: {with_email} ({with_email/total*100:.1f}%)")
    print("\nTop roles:")
    for r, c in sorted(by_role.items(), key=lambda x: -x[1])[:12]:
        print(f"  {c:>4}  {r}")
    print("\nTop schools:")
    for s, c in sorted(by_school.items(), key=lambda x: -x[1])[:10]:
        print(f"  {c:>4}  {s}")
    print("=" * 70)


# ============================================================================
# DEPENDENCY INSTALL HELPER
# ============================================================================

def install_dependencies(include_js: bool = False) -> None:
    import subprocess
    pkgs = ["requests", "beautifulsoup4", "lxml", "pandas", "openpyxl", "tqdm"]
    if include_js:
        pkgs.append("playwright")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *pkgs])
    if include_js:
        subprocess.check_call([sys.executable, "-m", "playwright", "install",
                               "chromium"])
    print("Dependencies installed.")


# ============================================================================
# CLI
# ============================================================================

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape staff/faculty directories from school or "
                    "organization websites.")
    p.add_argument("--url", help="District homepage or a single directory URL")
    p.add_argument("--name", default=None, help="District / school name")
    p.add_argument("--single-page", action="store_true",
                   help="Treat --url as one directory page (skip discovery)")
    p.add_argument("--js", action="store_true",
                   help="Enable JavaScript rendering fallback (Playwright)")
    p.add_argument("--output", default="output", help="Output directory")
    p.add_argument("--max-schools", type=int, default=0,
                   help="Limit number of schools (0 = all)")
    p.add_argument("--no-robots", action="store_true",
                   help="Do not consult robots.txt")
    p.add_argument("--delay", type=float, default=0.4,
                   help="Seconds between requests per host")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--install", action="store_true",
                   help="Install dependencies and exit")
    return p


def _in_notebook() -> bool:
    """True when running inside IPython / Jupyter / Google Colab."""
    try:
        from IPython import get_ipython  # type: ignore
        ip = get_ipython()
        if ip is None:
            return False
        # ZMQInteractiveShell = Jupyter/Colab kernel; TerminalInteractiveShell
        # = plain `ipython` in a terminal (treat that as CLI-capable).
        return ip.__class__.__name__ == "ZMQInteractiveShell"
    except Exception:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    # parse_known_args so stray environment args (e.g. Colab/Jupyter's
    # "-f /root/.../kernel.json") are ignored instead of crashing.
    args, unknown = _build_arg_parser().parse_known_args(argv)
    setup_logging(args.log_level)
    if unknown:
        logger.debug("Ignoring unrecognized args: %s", unknown)

    if args.install:
        install_dependencies(include_js=args.js)
        return 0

    if not args.url:
        _build_arg_parser().print_help()
        return 1

    config = ScraperConfig(
        use_javascript=args.js,
        output_dir=args.output,
        max_schools=args.max_schools,
        respect_robots=not args.no_robots,
        rate_limit_delay=args.delay,
    )

    if args.single_page:
        result = scrape_directory_page(args.url, args.name or "School",
                                       config=config)
    else:
        result = scrape_district(args.url, args.name, config=config)

    if result.get("success"):
        show_stats(result)
        return 0
    print("Scrape failed:", result.get("error"))
    return 2


if __name__ == "__main__" and not _in_notebook():
    # In a notebook (Colab/Jupyter) importing or running this file should only
    # define the functions -- call scrape_district(...) yourself in a cell.
    raise SystemExit(main())
