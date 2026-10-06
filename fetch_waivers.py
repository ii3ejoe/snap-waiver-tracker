#!/usr/bin/env python3
"""
USDA SNAP Time Limit Waiver scraper
====================================
Fetches the public USDA FNA/FNS time-limit waiver page, extracts every
state's response documents, and writes a JSON file the companion HTML
tracker can load (no browser CORS needed).

Intended for non-profits and advocacy groups:
  • Run locally:     python3 fetch_waivers.py
  • Or free on GitHub Actions on a schedule (see .github/workflows/fetch-waivers.yml)

Output: waivers.json (same folder, or path given with -o)

Dependencies: Python 3.8+ standard library only (urllib, html.parser, json, re).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_URL = "https://www.fna.usda.gov/snap/waivers/timelimit/2025-2029"
ALT_URLS = [
    "https://www.fns.usda.gov/snap/waivers/timelimit/2025-2029",
    "https://fns-prod.azureedge.us/snap/waivers/timelimit/2025-2029",
]

STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "District of Columbia", "Florida", "Georgia",
    "Guam", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas",
    "Utah", "Vermont", "Virgin Islands", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]
STATE_SET = set(STATES)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

USER_AGENT = (
    "Mozilla/5.0 (compatible; SNAP-Waiver-Tracker/1.0; "
    "+https://github.com/; non-profit advocacy research)"
)


def fetch_html(url: str, timeout: int = 45) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def fetch_with_fallback(primary: str) -> Tuple[str, str]:
    tried = []
    for url in [primary] + [u for u in ALT_URLS if u != primary]:
        try:
            html = fetch_html(url)
            if len(html) > 2000 and re.search(
                r"Response Documents|Time Limit Waivers|ABAWD|State Requests",
                html,
                re.I,
            ):
                return html, url
            tried.append(f"{url}: unexpected content")
        except (HTTPError, URLError, TimeoutError, OSError) as e:
            tried.append(f"{url}: {e}")
    raise RuntimeError("Could not fetch USDA page:\n  " + "\n  ".join(tried))


def parse_date(text: str) -> Tuple[str, str]:
    """Return (iso YYYY-MM-DD or '', original snippet)."""
    m = re.search(
        r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
        text or "",
    )
    if m:
        mon = MONTHS.get(m.group(1)[:3].lower())
        if mon:
            iso = f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
            return iso, m.group(0)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    if m:
        iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        return iso, m.group(0)
    return "", ""


def extract_fy(text: str) -> int:
    m = re.search(r"FY\s*(\d{4})", text or "", re.I)
    return int(m.group(1)) if m else 0


class WaiverParser(HTMLParser):
    """Extract state response docs from USDA <dl><dt>State</dt><dd>…</dd> layout."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.states: Dict[str, List[dict]] = {s: [] for s in STATES}
        self.starred: Dict[str, bool] = {}
        self.requests: List[dict] = []
        self.general: List[dict] = []
        self.page_updated = ""

        self._in_dt = False
        self._in_dd = False
        self._dt_buf = ""
        self._cur_state: Optional[str] = None
        self._in_a = False
        self._a_href = ""
        self._a_text = ""
        self._after_a_tail = ""
        self._collect_tail = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        if tag == "dt":
            self._in_dt = True
            self._dt_buf = ""
        elif tag == "dd":
            self._in_dd = True
        elif tag == "a" and self._in_dd and self._cur_state:
            self._in_a = True
            self._a_href = ad.get("href", "")
            self._a_text = ""
            self._after_a_tail = ""
            self._collect_tail = False
        elif tag == "a" and not self._in_dd:
            # zip / general links outside state blocks — capture later via regex fallback
            pass

    def handle_endtag(self, tag: str) -> None:
        if tag == "dt":
            name = re.sub(r"\s*[*:]+\s*$", "", self._dt_buf.strip())
            if name in STATE_SET:
                self._cur_state = name
                if "*" in self._dt_buf:
                    self.starred[name] = True
            else:
                self._cur_state = None
            self._in_dt = False
        elif tag == "dd":
            # Flush any pending link that was waiting on tail text
            if self._collect_tail:
                self._commit_link()
            self._in_dd = False
            self._cur_state = None
        elif tag == "a" and self._in_a:
            self._in_a = False
            self._collect_tail = True
            # Do not commit yet — date often follows the </a> as "(Sept. 20, 2024)"
        elif tag == "li" and self._collect_tail:
            self._commit_link()

    def handle_data(self, data: str) -> None:
        if self._in_dt:
            self._dt_buf += data
        if self._in_a:
            self._a_text += data
        elif self._collect_tail and self._cur_state:
            self._after_a_tail += data
            # Commit once we likely have the parenthetical date
            if re.search(r"\(\s*[A-Za-z]{3}", self._after_a_tail) and ")" in self._after_a_tail:
                self._commit_link()
            elif len(self._after_a_tail) > 100:
                self._commit_link()

    def _commit_link(self) -> None:
        if not self._cur_state or not self._a_href:
            self._after_a_tail = ""
            self._collect_tail = False
            return
        href = urljoin(self.base_url, self._a_href)
        if not href.lower().startswith("http"):
            self._after_a_tail = ""
            self._collect_tail = False
            return
        title = re.sub(r"\s+", " ", self._a_text).strip()
        if not title:
            self._after_a_tail = ""
            self._collect_tail = False
            return
        is_zip = bool(re.search(r"\.zip(\?|$)", href, re.I))
        looks = bool(
            re.search(
                r"waiver|response|termination|status update|FNA Response|FNS Response",
                title,
                re.I,
            )
            or re.search(r"\.pdf(\?|$)", href, re.I)
        )
        if not is_zip and not looks:
            self._after_a_tail = ""
            self._collect_tail = False
            return
        combined = f"{title} {self._after_a_tail}"
        iso, date_text = parse_date(combined)
        doc = {
            "key": href,
            "title": title,
            "fy": extract_fy(title),
            "date": iso,
            "dateText": date_text,
            "flagged": "*" in combined,
        }
        if is_zip:
            if not any(d["key"] == href for d in self.requests):
                self.requests.append(doc)
        else:
            lst = self.states[self._cur_state]
            if not any(d["key"] == href for d in lst):
                lst.append(doc)
        self._a_href = ""
        self._a_text = ""
        self._after_a_tail = ""
        self._collect_tail = False


def parse_page_updated(html: str) -> str:
    m = re.search(
        r"Page updated:\s*([A-Za-z]+\.?\s+\d{1,2},\s+\d{4})",
        html,
        re.I,
    )
    return m.group(1).strip() if m else ""


def parse_zip_requests(html: str, base_url: str) -> List[dict]:
    """Pull FY request zip files from the State Requests section."""
    out: List[dict] = []
    for m in re.finditer(
        r'<a\s+[^>]*href="([^"]+\.zip[^"]*)"[^>]*>([^<]*)</a>',
        html,
        re.I,
    ):
        href = urljoin(base_url, m.group(1))
        title = re.sub(r"\s+", " ", m.group(2)).strip() or "State requests (.zip)"
        # Include nearby parenthetical text if present
        tail = html[m.end() : m.end() + 80]
        tm = re.match(r"\s*(\([^)]+\))", tail)
        if tm:
            title = f"{title} {tm.group(1)}".strip()
        iso, date_text = parse_date(title)
        out.append(
            {
                "key": href,
                "title": title,
                "fy": extract_fy(title),
                "date": iso,
                "dateText": date_text,
                "flagged": False,
            }
        )
    # dedupe
    seen = set()
    uniq = []
    for d in out:
        if d["key"] not in seen:
            seen.add(d["key"])
            uniq.append(d)
    return uniq


def parse_general_notices(html: str, base_url: str) -> List[dict]:
    out: List[dict] = []
    # Status update / termination style links near Response Documents
    for m in re.finditer(
        r'<a\s+[^>]*href="([^"]+)"[^>]*>([^<]*(?:Status Update|Termination|Waiver of)[^<]*)</a>',
        html,
        re.I,
    ):
        href = urljoin(base_url, m.group(1))
        title = re.sub(r"\s+", " ", m.group(2)).strip()
        tail = html[m.end() : m.end() + 60]
        iso, date_text = parse_date(title + " " + tail)
        out.append(
            {
                "key": href,
                "title": title,
                "fy": extract_fy(title),
                "date": iso,
                "dateText": date_text,
                "flagged": False,
            }
        )
    seen = set()
    uniq = []
    for d in out:
        if d["key"] not in seen:
            seen.add(d["key"])
            uniq.append(d)
    return uniq


def build_payload(html: str, page_url: str) -> Dict[str, Any]:
    parser = WaiverParser(page_url)
    parser.feed(html)
    if parser._collect_tail:
        parser._commit_link()
    parser.close()

    page_updated = parse_page_updated(html) or parser.page_updated
    requests = parser.requests or parse_zip_requests(html, page_url)
    general = parse_general_notices(html, page_url)

    total_docs = sum(len(v) for v in parser.states.values())
    if total_docs == 0:
        raise RuntimeError(
            "Parsed USDA page but found zero state response documents. "
            "The page layout may have changed."
        )

    now = datetime.now(timezone.utc)
    return {
        "schemaVersion": 1,
        "source": "USDA FNA/FNS time limit waiver page",
        "pageUrl": page_url,
        "pageUpdated": page_updated,
        "fetchedAt": int(now.timestamp() * 1000),
        "fetchedAtIso": now.isoformat(),
        "states": parser.states,
        "starred": parser.starred,
        "requests": requests,
        "general": general,
        "summary": {
            "statesWithDocuments": sum(1 for v in parser.states.values() if v),
            "totalDocuments": total_docs,
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch USDA SNAP time-limit waiver data")
    ap.add_argument(
        "-u",
        "--url",
        default=DEFAULT_URL,
        help=f"USDA page URL (default: {DEFAULT_URL})",
    )
    ap.add_argument(
        "-o",
        "--output",
        default="waivers.json",
        help="Output JSON path (default: waivers.json)",
    )
    ap.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON",
    )
    args = ap.parse_args(argv)

    print(f"Fetching {args.url} …", file=sys.stderr)
    t0 = time.time()
    html, used = fetch_with_fallback(args.url)
    print(f"  got {len(html):,} bytes from {used} in {time.time() - t0:.1f}s", file=sys.stderr)

    payload = build_payload(html, used)
    out_path = Path(args.output)
    text = json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False)
    out_path.write_text(text + "\n", encoding="utf-8")

    s = payload["summary"]
    print(
        f"Wrote {out_path} — {s['statesWithDocuments']} states, "
        f"{s['totalDocuments']} documents"
        + (f"; page updated {payload['pageUpdated']}" if payload["pageUpdated"] else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
