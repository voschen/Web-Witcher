
import json
import logging
import datetime
import calendar
import argparse
import requests
from dotenv import load_dotenv
import os
from playwright.sync_api import sync_playwright

load_dotenv()
api_key = os.getenv("OPEN_ROUTER_API_KEY")

# ── Configuration ────────────────────────────────────────────────────────────

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generic web scraping agent for downloading PDF documents by target date."
    )
    parser.add_argument(
        "--date",
        default="January 2026",
        help="Target date or date range for document collection (default: 'January 2026')"
    )
    parser.add_argument(
        "--sources",
        default="sources.json",
        help="Path to JSON file containing list of sources (default: 'sources.json')"
    )
    parser.add_argument(
        "--output",
        default="pdfs_downloaded",
        help="Output directory for downloaded PDFs (default: 'pdfs_downloaded')"
    )
    return parser.parse_args()

args = parse_args()
TARGET_MONTH = args.date

PDF_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), args.output)
os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)

with open(args.sources, "r") as f:
    sources = json.load(f)

# PDF keywords that indicate an aggregate/site-wide document (never download these)
AGGREGATE_PDF_KEYWORDS = ["full version", "complete site-wide", "all sections", "complete collection"]


# ── Playwright helper ──────────────────────────────────────────────────────────

def _parse_target_range(target_month: str):
    """Parse target_month into (start_dt, end_dt) as datetime objects.

    Handles 'February 2026', 'February 20 2026 to March 26 2026',
    'after 29/3/2026', 'before 2026-03-29', 'from 01/01/2026', etc.
    For a single month, start_dt is the 1st and end_dt is the last day.
    """
    _FORMATS = (
        "%B %d %Y", "%B %Y", "%b %d %Y", "%b %Y",
        "%d %B %Y", "%d %b %Y",
        "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d",
    )

    def _try_parse(s: str) -> datetime.datetime:
        s = s.strip()
        for fmt in _FORMATS:
            try:
                return datetime.datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise ValueError(f"Cannot parse date: {s}")

    s = target_month.strip()

    for prefix in ("after ", "from "):
        if s.lower().startswith(prefix):
            date_str = s[len(prefix):].strip()
            start_dt = _try_parse(date_str)
            end_dt = datetime.datetime.now() + datetime.timedelta(days=365)
            return start_dt, end_dt

    if s.lower().startswith("before "):
        date_str = s[len("before "):].strip()
        end_dt = _try_parse(date_str)
        start_dt = datetime.datetime(2000, 1, 1)
        return start_dt, end_dt

    if " to " in s:
        start_str, end_str = s.split(" to ", 1)
        start_dt = _try_parse(start_str)
        end_dt = _try_parse(end_str)
        return start_dt, end_dt

    # Single month/date
    start_dt = _try_parse(s)
    last_day = calendar.monthrange(start_dt.year, start_dt.month)[1]
    end_dt = start_dt.replace(day=last_day)
    return start_dt, end_dt


def _page_is_past_target(body_text: str, target_month: str, link_texts: str = "") -> bool:
    """Return True if the page shows only content OLDER than target_month.
    Used to decide whether to override a premature LLM 'done'.
    """
    try:
        start_dt, _end_dt = _parse_target_range(target_month)
    except ValueError:
        return False  # can't parse, don't override
    combined = (body_text + " " + link_texts).lower()
    check_dt = start_dt
    while (check_dt.year, check_dt.month) <= (_end_dt.year, _end_dt.month):
        if check_dt.strftime("%B %Y").lower() in combined:
            return False
        if check_dt.month == 12:
            check_dt = check_dt.replace(year=check_dt.year + 1, month=1)
        else:
            check_dt = check_dt.replace(month=check_dt.month + 1)
    for year in range(start_dt.year - 1, start_dt.year + 1):
        for mo in range(1, 13):
            if (year, mo) >= (start_dt.year, start_dt.month):
                continue
            older_month = datetime.date(year, mo, 1).strftime("%B %Y")
            if older_month.lower() in combined:
                return True
    return False


def _page_is_before_target(body_text: str, target_month: str, link_texts: str = "") -> bool:
    """Return True if the page shows ONLY content NEWER than target_month
    (target itself not present, no older content either).
    Used to detect that the target month is on a subsequent page.
    """
    try:
        start_dt, end_dt = _parse_target_range(target_month)
    except ValueError:
        return False
    combined = (body_text + " " + link_texts).lower()
    check_dt = start_dt
    while (check_dt.year, check_dt.month) <= (end_dt.year, end_dt.month):
        if check_dt.strftime("%B %Y").lower() in combined:
            return False
        if check_dt.month == 12:
            check_dt = check_dt.replace(year=check_dt.year + 1, month=1)
        else:
            check_dt = check_dt.replace(month=check_dt.month + 1)
    has_newer = False
    for year in range(end_dt.year, end_dt.year + 2):
        for mo in range(1, 13):
            if (year, mo) <= (end_dt.year, end_dt.month):
                continue
            newer_month = datetime.date(year, mo, 1).strftime("%B %Y")
            if newer_month.lower() in combined:
                has_newer = True
                break
        if has_newer:
            break
    if not has_newer:
        return False
    # Make sure NO older months appear (before range start), otherwise it's mixed
    for year in range(start_dt.year - 1, start_dt.year + 1):
        for mo in range(1, 13):
            if (year, mo) >= (start_dt.year, start_dt.month):
                continue
            older_month = datetime.date(year, mo, 1).strftime("%B %Y")
            if older_month.lower() in combined:
                return False
    return True

def get_page_state(page):
    """Snapshot the current page to hand off to the LLM."""
    # Wait briefly for JS-rendered content to appear
    page.wait_for_timeout(500)

    links = page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
            .map(a => ({ text: a.innerText.trim().slice(0, 120), href: a.href, title: (a.getAttribute('title') || '').slice(0, 80) }))
            .filter(l => l.text || l.href)
            .slice(0, 150)
    """)

    body_text = page.evaluate("""
        () => {
            const el = document.body;
            return el ? el.innerText.slice(0, 8000) : '';
        }
    """)

    # Explicitly find pagination links so the LLM always knows the exact next-page URL
    pagination = page.evaluate("""
        () => {
            const results = {};
            const allLinks = Array.from(document.querySelectorAll('a[href]'));
            for (const a of allLinks) {
                const t = a.innerText.trim().toLowerCase();
                if (t === 'next page' || t === 'next' || t === '>' || t === '\u203a' || t === '\u00bb') {
                    results.next = a.href;
                    break;
                }
            }
            if (!results.next) {
                for (const a of allLinks) {
                    const label = (a.getAttribute('aria-label') || a.getAttribute('title') || '').toLowerCase();
                    if (label.includes('next')) {
                        results.next = a.href;
                        break;
                    }
                }
            }
            if (!results.next) {
                const rel = document.querySelector('a[rel="next"]');
                if (rel) results.next = rel.href;
            }
            if (!results.next) {
                const sel = '.pagination-next a, .next-page a, li.next a, [class*="next"] a, a[class*="next"]';
                const candidates = document.querySelectorAll(sel);
                if (candidates.length > 0) results.next = candidates[0].href;
            }
            return results;
        }
    """)

    # Search for button elements with "next" text (JS-driven pagination)
    button_pagination = page.evaluate("""
        () => {
            const results = {};
            const buttons = Array.from(document.querySelectorAll('button'));
            for (const el of buttons) {
                if (el.innerText.trim().toLowerCase().includes('next')) {
                    const cls = el.className
                        ? '.' + el.className.trim().split(/\\s+/).join('.')
                        : '';
                    results.next_button_selector = 'button' + cls;
                    results.next_button_text = el.innerText.trim();
                    break;
                }
            }
            // Fallback: known AEM/CMP pagination button class
            if (!results.next_button_selector) {
                const fallback = document.querySelector('.cmp-search-results__pagination__button--next');
                if (fallback) {
                    results.next_button_selector = '.cmp-search-results__pagination__button--next';
                    results.next_button_text = fallback.innerText.trim();
                }
            }
            return results;
        }
    """)
    if button_pagination.get("next_button_selector"):
        pagination["next_button_selector"] = button_pagination["next_button_selector"]
        pagination["next_button_text"] = button_pagination.get("next_button_text", "")

    crawl_type = detect_crawl_type(page)

    # For API-driven sites, look for JS-triggered pagination elements
    if crawl_type == "api":
        api_pagination = page.evaluate("""
            () => {
                const results = {};
                const candidates = Array.from(document.querySelectorAll('[onclick], [data-page]'));
                for (const el of candidates) {
                    const text = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
                    const onclick = el.getAttribute('onclick') || '';
                    if (text.includes('next') || onclick.toLowerCase().includes('next') || onclick.includes('page')) {
                        results.next_type = 'api_trigger';
                        const tag = el.tagName.toLowerCase();
                        const id = el.id ? '#' + el.id : '';
                        const cls = el.className ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
                        results.next_selector = tag + id + cls;
                        break;
                    }
                }
                return results;
            }
        """)
        if api_pagination.get("next_type"):
            pagination["next_type"] = api_pagination["next_type"]
            pagination["next_selector"] = api_pagination["next_selector"]

    return {
        "url": page.url,
        "title": page.title(),
        "body_text": body_text,
        "links": links,
        "pagination": pagination,
        "crawl_type": crawl_type,
    }


# ── Publications-page finder ─────────────────────────────────────────────────

_PUBLICATIONS_KEYWORDS = [
    "publications", "press releases", "press release", "news",
    "research", "standards", "documents", "resources",
    "consultation papers", "reports", "speeches", "working papers",
    "library", "media", "updates",
]

def _find_publications_url(page) -> str | None:
    """Return a URL to navigate to if the current page is NOT a publications listing.
    Returns None if the page already looks like a dated listing.
    """
    body_text = page.evaluate("""
        () => document.body ? document.body.innerText.slice(0, 4000) : ''
    """)
    body_lower = body_text.lower()

    # Build dynamic date keywords from TARGET_MONTH range (6 months before start → 6 months after end)
    listing_date_keywords = []
    try:
        _start, _end = _parse_target_range(TARGET_MONTH)
        _cursor = _start
        for _ in range(6):
            if _cursor.month == 1:
                _cursor = _cursor.replace(year=_cursor.year - 1, month=12)
            else:
                _cursor = _cursor.replace(month=_cursor.month - 1)
        _window_start = _cursor
        _cursor = _end
        for _ in range(6):
            if _cursor.month == 12:
                _cursor = _cursor.replace(year=_cursor.year + 1, month=1)
            else:
                _cursor = _cursor.replace(month=_cursor.month + 1)
        _window_end = _cursor
        _cur = _window_start
        while (_cur.year, _cur.month) <= (_window_end.year, _window_end.month):
            listing_date_keywords.append(_cur.strftime("%b %Y").lower())
            listing_date_keywords.append(_cur.strftime("%B %Y").lower())
            if _cur.month == 12:
                _cur = _cur.replace(year=_cur.year + 1, month=1)
            else:
                _cur = _cur.replace(month=_cur.month + 1)
    except Exception:
        # Fallback: generic year strings for current and adjacent years
        _now = datetime.datetime.now()
        for _yr in range(_now.year - 1, _now.year + 2):
            listing_date_keywords.append(str(_yr))

    # Static indicators that we are already on a listing page (structural/strong)
    _static_listing_kws = [
        "showing results", "next page", "previous page",
        "page 1", "page 2", "results 1-",
    ]
    has_structural_listing = any(kw in body_lower for kw in _static_listing_kws)

    # Weak indicators: date/year mentions (could be framework docs, not a real listing)
    has_date_listing = any(kw in body_lower for kw in listing_date_keywords)
    if not has_date_listing:
        import re as _re
        if len(_re.findall(r'\b20[0-2]\d\b', body_text)) >= 3:
            has_date_listing = True

    # If strong structural signals are present, this IS a listing — stay here
    if has_structural_listing:
        return None

    # Not a listing — or only weak date signals — search for a publications link.
    # Exclude forum, search, and generic research index pages — they're not listings.
    _EXCLUDE_HREF_PATTERNS = ["forum/research", "forum/", "/search", "?m=", "&m=", "?q=", "&q="]
    links = page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
            .map(a => ({ text: a.innerText.trim().toLowerCase(), href: a.href }))
            .filter(l => l.text || l.href)
    """)
    for kw in _PUBLICATIONS_KEYWORDS:
        for link in links:
            text = link["text"]
            href = link["href"].lower()
            if any(ex in href for ex in _EXCLUDE_HREF_PATTERNS):
                continue
            if kw in text or kw in href:
                return link["href"]
    # If we had weak date signals but found no publications link, assume the
    # current page IS the listing (dates appear because it lists dated content).
    return None


# ── Crawl-type detection ──────────────────────────────────────────────────────

def detect_crawl_type(page) -> str:
    """Detect whether pagination on the current page is static, dynamic, or API-driven.

    Uses the already-rendered Playwright page (no extra HTTP request):
      - 'static'  : DOM has <main>/<article> AND a real <a href> next-page link
      - 'api'     : DOM has <main>/<article> but next link href is '#' or 'javascript:'
      - 'dynamic' : DOM has no <main>/<article> (content is JS-rendered)
    Defaults to 'dynamic' on any error.
    """
    try:
        has_main = page.query_selector("main, article")
        if not has_main:
            return "dynamic"

        next_href = page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const a of links) {
                    const text = a.innerText.trim().toLowerCase();
                    const href = a.getAttribute('href') || '';
                    if (text.includes('next') || href.toLowerCase().includes('next')) {
                        return href;
                    }
                }
                return null;
            }
        """)

        if next_href is None:
            return "dynamic"

        href = next_href.strip()
        if href.startswith("#") or href.lower().startswith("javascript:"):
            return "api"

        return "static"
    except Exception:
        return "dynamic"


# ── Date-filter applicator ───────────────────────────────────────────────────

# Common label patterns for "from" and "to" date filter inputs (case-insensitive)
_DATE_FROM_LABELS = ["published after", "published from", "date from", "from date", "start date", "date after"]
_DATE_TO_LABELS   = ["published before", "published to",   "date to",   "to date",   "end date",   "date before", "until", "till"]
# Short labels that must match the ENTIRE label (not just a substring) to avoid
# false positives like "jump to..." matching "to".
_DATE_FROM_LABELS_EXACT = ["from"]
_DATE_TO_LABELS_EXACT   = ["to"]

def _apply_date_filters(page, target_month: str) -> bool:
    """Detect and fill date-range filter inputs on the current listing page.

    Scans for <input type="date"> or text inputs labelled with common
    'Published After' / 'Published Before' patterns, fills them with the
    parsed target range, then submits the form.  Returns True if any filter
    was applied.
    """
    try:
        start_dt, end_dt = _parse_target_range(target_month)
    except Exception:
        return False

    # If the current URL already contains both date filter params matching the target,
    # the filter is already applied — skip re-filling to avoid form.submit() breaking it.
    current_url_check = page.url
    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs, urlencode as _urlencode
    _qs = _parse_qs(_urlparse(current_url_check).query)
    def _qs_val(d, *keys):
        for k in keys:
            if k in d:
                return d[k][0]
        return None
    _from_val = _qs_val(_qs, "fromDate", "from", "date_from", "startdate", "start_date")
    _till_val = _qs_val(_qs, "tillDate", "till", "toDate", "date_to", "enddate", "end_date")
    if _from_val and _till_val:
        print(f"  ↳ Date filter: URL already contains filter params (from={_from_val!r}, till={_till_val!r}) — skipping re-fill")
        return True

    # Wait for JS-rendered filter widgets to appear (domcontentloaded is too early)
    page.wait_for_timeout(1000)

    # Detect the expected date format from surrounding page text / placeholders
    page_date_hint = page.evaluate("""
        () => {
            // Look for format hints like "(dd/mm/yyyy)" in labels, headings, nearby text
            const body = document.body ? document.body.innerText : '';
            const m = body.match(/\\(?(dd[\\/\\-]mm[\\/\\-]yyyy|mm[\\/\\-]dd[\\/\\-]yyyy|yyyy[\\/\\-]mm[\\/\\-]dd)\\)?/i);
            if (m) return m[1].toLowerCase();
            // Check placeholders of date inputs
            const inputs = document.querySelectorAll('input[type="text"], input[type="date"]');
            for (const inp of inputs) {
                const ph = (inp.placeholder || '').toLowerCase();
                if (ph.match(/dd[\\/\\-]mm[\\/\\-]yyyy/)) return 'dd/mm/yyyy';
                if (ph.match(/mm[\\/\\-]dd[\\/\\-]yyyy/)) return 'mm/dd/yyyy';
                if (ph.match(/yyyy[\\/\\-]mm[\\/\\-]dd/)) return 'yyyy-mm-dd';
            }
            return null;
        }
    """)
    if page_date_hint:
        print(f"  ↳ Date filter: detected format hint '{page_date_hint}'")

    def _format_date(dt, hint):
        """Format a datetime according to the detected page date format hint."""
        if hint and 'dd' in hint:
            sep = '/' if '/' in hint else '-'
            if hint.startswith('dd'):
                return dt.strftime(f"%d{sep}%m{sep}%Y")
            elif hint.startswith('mm'):
                return dt.strftime(f"%m{sep}%d{sep}%Y")
            elif hint.startswith('yyyy'):
                return dt.strftime(f"%Y{sep}%m{sep}%d")
        # Default: yyyy-mm-dd (works for <input type="date"> and most sites)
        return dt.strftime("%Y-%m-%d")

    start_str = _format_date(start_dt, page_date_hint)
    # Only send end_dt if it is not the open-ended sentinel (1 year from now)
    end_sentinel = datetime.datetime.now() + datetime.timedelta(days=300)
    end_str = _format_date(end_dt, page_date_hint) if end_dt < end_sentinel else ""

    # Collect all text/date inputs with their associated label text
    inputs = page.evaluate("""
        () => {
            const results = [];
            const inputs = Array.from(document.querySelectorAll('input[type="date"], input[type="text"], input[type="search"]'));
            for (const inp of inputs) {
                // Get label via <label for=...>, aria-label, placeholder, or surrounding text
                let label = '';
                if (inp.id) {
                    const lbl = document.querySelector('label[for="' + inp.id + '"]');
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) label = inp.getAttribute('aria-label') || '';
                if (!label) label = inp.getAttribute('placeholder') || '';
                if (!label) {
                    // walk up to find a nearby label-like element
                    let el = inp.parentElement;
                    for (let i = 0; i < 3 && el; i++, el = el.parentElement) {
                        const txt = el.innerText ? el.innerText.trim().slice(0, 80) : '';
                        if (txt) { label = txt; break; }
                    }
                }
                let sel;
                if (inp.id) {
                    sel = 'input#' + inp.id;
                } else if (inp.name) {
                    sel = 'input[name="' + inp.name.replace(/"/g, '\\"') + '"]';
                } else if (inp.className) {
                    sel = 'input.' + inp.className.trim().split(/\\s+/).join('.');
                } else {
                    continue; // can't reliably identify this input
                }
                results.push({ label: label.toLowerCase(), selector: sel, name: inp.name || '', type: inp.type || '' });
            }
            return results;
        }
    """)

    if inputs:
        print(f"  ↳ Date filter: found {len(inputs)} input(s):")
        for i in inputs[:10]:
            print(f"      {i['selector']} [type={i['type']} name={i.get('name', '')!r} label={i['label'][:50]!r}]")
    else:
        # No inputs at domcontentloaded+3s — hard to filter programmatically
        print(f"  ↳ Date filter: no inputs found in DOM after wait.")
        return False

    from_sel = None
    to_sel   = None

    # Name-attribute keywords for from/to date fields
    _NAME_FROM_PATTERNS = ["date_after", "date_from", "from_date", "start_date", "startdate", "dps_date_after", "dateafter", "datefrom"]
    _NAME_TO_PATTERNS   = ["date_before", "date_to", "to_date", "end_date", "enddate", "dps_date_before", "datebefore", "dateto", "till"]

    # Placeholder/label tokens that indicate a date entry field (not a keyword field)
    _DATE_FORMAT_HINTS = ["yyyy-mm-dd", "dd/mm/yyyy", "mm/dd/yyyy", "dd-mm-yyyy", "mm-dd-yyyy"]

    # Pass 1: match by label text
    for inp in inputs:
        label = inp["label"]
        if not from_sel:
            for kw in _DATE_FROM_LABELS:
                if kw in label:
                    from_sel = inp["selector"]
                    break
            if not from_sel:
                for kw in _DATE_FROM_LABELS_EXACT:
                    if label.strip().strip(':').strip() == kw:
                        from_sel = inp["selector"]
                        break
        if not to_sel:
            for kw in _DATE_TO_LABELS:
                if kw in label:
                    to_sel = inp["selector"]
                    break
            if not to_sel:
                for kw in _DATE_TO_LABELS_EXACT:
                    if label.strip().strip(':').strip() == kw:
                        to_sel = inp["selector"]
                        break

    # Pass 1b: match by input name attribute
    # Short names that must match exactly (not as substrings) to avoid false positives
    _NAME_FROM_EXACT = ["from"]
    _NAME_TO_EXACT   = ["to", "till"]
    if not from_sel or not to_sel:
        for inp in inputs:
            name = inp.get("name", "").lower()
            if not from_sel:
                for pat in _NAME_FROM_PATTERNS:
                    if pat in name:
                        from_sel = inp["selector"]
                        print(f"  ↳ Date filter: matched from-field by name '{inp['name']}'")
                        break
                if not from_sel:
                    if name in _NAME_FROM_EXACT:
                        from_sel = inp["selector"]
                        print(f"  ↳ Date filter: matched from-field by exact name '{inp['name']}'")
            if not to_sel:
                for pat in _NAME_TO_PATTERNS:
                    if pat in name:
                        to_sel = inp["selector"]
                        print(f"  ↳ Date filter: matched to-field by name '{inp['name']}'")
                        break
                if not to_sel:
                    if name in _NAME_TO_EXACT:
                        to_sel = inp["selector"]
                        print(f"  ↳ Date filter: matched to-field by exact name '{inp['name']}'")


    # Pass 2: positional fallback on type="date" or date-format placeholder inputs
    # Exclude inputs whose name/label/selector suggests page navigation (not date filtering)
    if not from_sel and not to_sel:
        _NAV_KEYWORDS = ["page", "jump", "goto", "listnav"]
        date_inputs = [
            i for i in inputs
            if (
                i.get("type") == "date"
                or any(hint in i.get("label", "").lower() for hint in _DATE_FORMAT_HINTS)
            )
            and not any(kw in i.get("name", "").lower() for kw in _NAV_KEYWORDS)
            and not any(kw in i.get("label", "").lower() for kw in _NAV_KEYWORDS)
            and not any(kw in i.get("selector", "").lower() for kw in _NAV_KEYWORDS)
        ]
        if len(date_inputs) >= 2:
            from_sel = date_inputs[0]["selector"]
            to_sel   = date_inputs[1]["selector"]
            print(f"  ↳ Date filter: no label/name match — using positional fallback ({from_sel}, {to_sel})")
        elif len(date_inputs) == 1:
            from_sel = date_inputs[0]["selector"]
            print(f"  ↳ Date filter: no label/name match — using single date input as from-field ({from_sel})")

    if not from_sel and not to_sel:
        print(f"  ↳ Date filter: no date inputs detected on this page.")
        return False

    def _fill_input(sel: str, value: str) -> bool:
        """Try multiple strategies to fill a date input.
        Returns True if the value was set successfully.
        """
        # Strategy 1: jQuery datepicker — set via API AND trigger onSelect/onClose callbacks
        try:
            dp_result = page.evaluate(
                """([sel, val]) => {
                    const el = document.querySelector(sel);
                    if (!el) return {ok: false, reason: 'element not found'};
                    if (typeof jQuery === 'undefined') return {ok: false, reason: 'no jQuery'};
                    const $el = jQuery(el);
                    if (!$el.data('datepicker') && !$el.hasClass('hasDatepicker'))
                        return {ok: false, reason: 'not a datepicker'};
                    try {
                        // Parse the value
                        let parts, d;
                        if (val.match(/^\\d{2}[/\\-]\\d{2}[/\\-]\\d{4}$/)) {
                            parts = val.split(/[/\\-]/);
                            d = new Date(parseInt(parts[2]), parseInt(parts[1])-1, parseInt(parts[0]));
                        } else if (val.match(/^\\d{4}[/\\-]\\d{2}[/\\-]\\d{2}$/)) {
                            parts = val.split(/[/\\-]/);
                            d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
                        } else {
                            d = new Date(val);
                        }
                        if (isNaN(d.getTime())) return {ok: false, reason: 'invalid date: ' + val};

                        // Set the date via API
                        $el.datepicker('setDate', d);

                        // Manually fire onSelect callback (setDate does NOT trigger it)
                        var inst = $el.data('datepicker') || jQuery.datepicker._getInst(el);
                        var dateStr = jQuery.datepicker.formatDate(
                            inst ? inst.settings.dateFormat || jQuery.datepicker._defaults.dateFormat : 'dd/mm/yy',
                            d
                        );
                        var onSelect = inst && inst.settings && inst.settings.onSelect;
                        if (typeof onSelect === 'function') {
                            onSelect.call(el, dateStr, inst);
                        }
                        // Also fire onClose if present
                        var onClose = inst && inst.settings && inst.settings.onClose;
                        if (typeof onClose === 'function') {
                            onClose.call(el, dateStr, inst);
                        }
                        // Fire DOM change event for any non-datepicker listeners
                        el.dispatchEvent(new Event('change', { bubbles: true }));

                        var readback = el.value;
                        return {ok: true, strategy: 'jquery-datepicker', readback: readback,
                                hasOnSelect: typeof onSelect === 'function',
                                hasOnClose: typeof onClose === 'function'};
                    } catch(e) {
                        return {ok: false, reason: 'datepicker error: ' + e.message};
                    }
                }""",
                [sel, value],
            )
            if dp_result.get("ok"):
                print(f"      [fill] jQuery datepicker OK: readback={dp_result.get('readback')!r}, "
                      f"onSelect={dp_result.get('hasOnSelect')}, onClose={dp_result.get('hasOnClose')}")
                return True
            else:
                print(f"      [fill] jQuery datepicker skip: {dp_result.get('reason')}")
        except Exception as e:
            print(f"      [fill] jQuery datepicker error: {e}")

        # Strategy 2: click to focus, clear, then type the value character by character
        try:
            page.click(sel, timeout=3000)
            page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (el) { el.value = ''; }
            }""", sel)
            page.type(sel, value, delay=50)
            # Dismiss any open datepicker calendar by pressing Escape, then fire change
            try:
                page.press(sel, "Escape")
            except Exception:
                pass
            page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (el) el.dispatchEvent(new Event('change', { bubbles: true }));
            }""", sel)
            readback = page.evaluate(f"document.querySelector('{sel}')?.value")
            print(f"      [fill] click+type OK: readback={readback!r}")
            return True
        except Exception as e:
            print(f"      [fill] click+type failed: {e}")

        # Strategy 3: standard page.fill
        try:
            page.fill(sel, value, timeout=5000)
            readback = page.evaluate(f"document.querySelector('{sel}')?.value")
            print(f"      [fill] page.fill OK: readback={readback!r}")
            return True
        except Exception as e:
            print(f"      [fill] page.fill failed: {e}")

        # Strategy 4: JS native value setter + events
        try:
            page.evaluate(
                """([sel, val]) => {
                    const el = document.querySelector(sel);
                    if (!el) return;
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(el, val);
                    el.dispatchEvent(new Event('input',  { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                [sel, value],
            )
            readback = page.evaluate(f"document.querySelector('{sel}')?.value")
            print(f"      [fill] JS setter OK: readback={readback!r}")
            return True
        except Exception as e:
            print(f"  ⚠ Date filter fill failed for '{sel}': {e}")
            return False

    # Capture URL BEFORE any fills — datepicker onClose callbacks may change
    # the URL during fills (via pushState), so we need the original to detect changes.
    original_url = page.url
    print(f"  ↳ Date filter: original URL (before fills) = {original_url}")

    applied = False
    if from_sel:
        if _fill_input(from_sel, start_str):
            print(f"  ↳ Date filter: filled '{from_sel}' (from) with {start_str}")
            applied = True
        else:
            print(f"  ⚠ Date filter fill failed for from-field '{from_sel}'")

    if to_sel and end_str:
        if _fill_input(to_sel, end_str):
            print(f"  ↳ Date filter: filled '{to_sel}' (to) with {end_str}")
            applied = True
        else:
            print(f"  ⚠ Date filter fill failed for to-field '{to_sel}'")

    if applied:
        # Wait for datepicker onSelect/onClose callbacks to fire and update the page
        page.wait_for_timeout(800)

        current_url = page.url
        print(f"  ↳ Date filter: URL after fills = {current_url}")
        url_changed = current_url != original_url

        if url_changed:
            # The datepicker callbacks already updated the URL (via pushState/replaceState
            # or navigation). This is common with jQuery UI datepicker sites like BIS.
            # Do NOT call form.submit() — it would use native <input name> params which
            # may differ from the JS-constructed params, breaking the filter.
            print(f"  ↳ Date filter: URL changed by datepicker callbacks — navigating to apply filter")
            # Force a full page load with the callback-constructed URL to ensure
            # the server processes the filter parameters.
            page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
        else:
            # URL didn't change — need explicit form submission
            submit_info = page.evaluate("""
                (fromSel) => {
                    const input = document.querySelector(fromSel);
                    if (!input) return {found: false, reason: 'input element not found'};
                    let form = input.closest('form');
                    if (!form) return {found: false, reason: 'no parent <form> found'};
                    const candidates = Array.from(form.querySelectorAll(
                        'button[type="submit"], input[type="submit"], button'
                    ));
                    const btnTexts = candidates.map(b => (b.innerText || b.value || '').trim().toLowerCase());
                    for (let i = 0; i < candidates.length; i++) {
                        const t = btnTexts[i];
                        if (t.includes('search') || t.includes('filter') || t.includes('apply') || t.includes('go') || t.includes('submit')) {
                            candidates[i].click();
                            return {found: true, clicked: t, formAction: form.action || '(none)'};
                        }
                    }
                    const fallback = form.querySelector('button[type="submit"], input[type="submit"]');
                    if (fallback) {
                        fallback.click();
                        return {found: true, clicked: (fallback.innerText || fallback.value || '').trim(), formAction: form.action || '(none)'};
                    }
                    try { form.submit(); return {found: true, clicked: 'form.submit()', formAction: form.action || '(none)'}; } catch(e) {}
                    return {found: false, reason: 'no submit button in form', formAction: form.action || '(none)', buttonTexts: btnTexts.slice(0, 5)};
                }
            """, from_sel or to_sel)
            print(f"  ↳ Date filter: submit attempt = {submit_info}")

            if not submit_info.get("found"):
                try:
                    page.press(from_sel or to_sel, "Enter")
                    print(f"  ↳ Date filter: pressed Enter as fallback")
                except Exception as e:
                    print(f"  ⚠ Date filter: Enter press failed: {e}")

            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(800)

        # Log the post-submit state
        post_url = page.url
        print(f"  ↳ Date filter: post-submit URL = {post_url}")
        page_text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 800) : ''")
        first_items = page.evaluate("""
            () => {
                const body = document.body ? document.body.innerText : '';
                const countMatch = body.match(/(\\d+)\\s*items?/i);
                return {
                    itemCount: countMatch ? countMatch[1] : 'unknown',
                    snippet: body.slice(0, 400)
                };
            }
        """)
        print(f"  ↳ Date filter: result count = {first_items.get('itemCount', '?')} items")

        got_zero_results = (
            "results 0" in page_text.lower()
            or "0 results" in page_text.lower()
            or "no results" in page_text.lower()
            or "0 items" in page_text.lower()
            or "search results for ''" in page_text.lower()
        )
        if got_zero_results:
            print(f"  ⚠ Date filter submission returned 0 results — navigating back to listing.")
            page.goto(original_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(500)
            return False

        print(f"  ↳ Date filter applied — results narrowed to on/after {start_str}")

    return applied


# ── LLM helper ────────────────────────────────────────────────────────────────

def build_system_prompt(target_month: str) -> str:
    return f"""\
You are a web-scraping agent. Your only goal is to find and download PDF documents
from target websites.

At every step you receive the current page state and must reply with a single JSON
action object — no extra text, no markdown fences.

Available actions:
  {{"action": "navigate",     "url": "<full url>",          "reason": "<why>"}}
  {{"action": "click",        "selector": "<css selector>", "reason": "<why>"}}
  {{"action": "fill",         "selector": "<css selector>", "value": "<text to type>", "reason": "<why>"}}
  {{"action": "download_pdf", "url": "<pdf url>",           "filename": "<name>.pdf", "reason": "<why>"}}
  {{"action": "done",                                        "reason": "<why>"}}

Rules:
0. INITIAL NAVIGATION: Before doing anything else, check whether the current page is
   a publications listing page — meaning it shows multiple dated publication entries
   or a paginated search/archive results section.
   If it is NOT (e.g. it is a homepage, about page, or general landing page with no
   dated publication list), look in the navigation or body links for entries such as
   'Publications', 'Press Releases', 'News', 'Research', 'Standards', 'Documents',
   'Resources', 'Consultation papers', or similar, and navigate there first.
   Do NOT start searching for {target_month} items until you are on a page that
   actually lists publications with visible dates.

1. TARGET: Download ALL PDF publications released in {target_month} only.

2. TWO PAGE TYPES — know where you are:
   - LISTING PAGE: shows multiple publications with dates (e.g. search results, archive).
     A listing page may have a featured/highlighted section at the top AND a separate
     paginated search results section below. Treat them independently.
   - DETAIL PAGE: shows a single publication (title, description, download links).
   Your task flows: listing → detail (to get PDF) → back to listing → next listing page.

3. ON A LISTING PAGE — do this in order:
   a. Look at the PAGINATED SEARCH RESULTS section (not the featured/hero section at top).
      Check the dates of the items in the search results.
   b. SKIP ALL ITEMS THAT ARE NOT FROM {target_month}. Do NOT visit their detail pages.
      Do NOT download anything from them. Completely ignore them.
      THIS IS ABSOLUTE — if the PUBLICATION DATE displayed next to a document is not
      {target_month}, do NOT download it, period.
      - Do NOT guess that a file "might" be from {target_month} just because the filename
        has no date in it.
      - Do NOT download to "confirm" the date — if the listing says March 2026, it is
        March 2026.
      - Do NOT visit a detail page for a non-target-month item at all.
      The displayed publication date is definitive and final. There are zero exceptions.
   c. For every {target_month} item in the search results: navigate to its detail page,
      download any PDF, then return to this listing page.
   d. After handling all {target_month} items on this page (or if there are none):
      If a NEXT BUTTON SELECTOR is provided, use action=click with that exact selector —
      this site uses a JavaScript button for pagination, not a link.
      Otherwise, look for 'Next', 'Next page', '>' or a numbered pagination link,
      find its exact URL in the links list and use action=navigate. Never stop at
      one page if pagination exists.
   e. If the search results on this page show dates NEWER than {target_month}:
      there are ZERO items to process here. Go straight to the Next page link.
      DO NOT visit any detail pages. DO NOT download any PDFs.
      Example: targeting February 2026 and seeing March 2026 results → navigate Next
      immediately, without visiting any of those March pages.
      CRITICAL: Seeing only NEWER dates means {target_month} is on the NEXT page —
      do NOT call done. You must navigate forward to find it.

4. ON A DETAIL PAGE — do this in order:
   a. Look for a PDF download link whose title or context clearly corresponds to the
      publication you navigated here for. Download ONLY that PDF.
      Do NOT download other PDFs linked on the detail page (e.g. methodology documents,
      annexes from other years, or reference guides) — these are supporting materials,
      not the target publication. If you are unsure whether a PDF matches the publication
      you came here for, skip it and return to the listing page.
      If a previous download attempt for this publication already failed (404 or other
      error), do NOT download any other PDF on the same page as a substitute. There is
      no fallback. Accept that this publication has no downloadable PDF and return to
      the listing page.
      Do NOT download aggregate or "full version" documents (e.g. "Complete site-wide
      standards", "All sections", "Full collection") — these are site-wide
      compilations, not individual target-month publications.
   b. If no PDF link is visible in the links list, accept it and go back to listing.
   c. NEVER call action=done from a detail page. Always return to the listing page
      first, so you can check for more {target_month} items and pagination.
      When returning to the listing page, always use the exact Source URL shown at
      the top of every message — never reconstruct it from memory.

5. EARLY STOP: You may only call action=done if:
   - You are on a LISTING PAGE, AND
   - The search results on this page show dates OLDER than {target_month} (e.g. January
     2026 or earlier when targeting February 2026), AND
   - You have already paginated past all {target_month} content.
   If the search results still show dates NEWER than or equal to {target_month},
   you MUST keep paginating. Do NOT call done.

6. PAGINATION DIRECTION: Publications are sorted newest-first. 'Next page' always
   goes to OLDER content in the search results. So:
   - Page 1 might show April/March 2026
   - Page 2 might show March/February 2026
   - Page 3 might show February/January 2026
   If you are targeting February 2026 and page 1 shows March 2026, you MUST go to
   the next page — the target month is ahead of you.

7. ALREADY VISITED PAGES: You will be told which detail page URLs you already visited.
   Do NOT navigate to any URL in that list again — there is nothing new to find there.

8. ALREADY DOWNLOADED: You will be told which PDF URLs were already saved.
   Do NOT issue download_pdf for any URL in that list.

9. NEVER FABRICATE URLs: Only use URLs that appear exactly as-is in the provided links
   list. Do NOT construct, guess, or modify URLs. If you cannot see a PDF link on a
   detail page, the document may not have a downloadable PDF — accept it and go back.

10. Never invent selectors; only use ones visible in the provided page state.
11. Prefer direct PDF links over multi-click navigation when possible.
12. NEVER use action=navigate with a PDF URL. Always use action=download_pdf for any
    URL ending in .pdf or pointing to a document file. Using navigate on a PDF URL
    will crash the browser.
13. If a PDF URL fails (404 or other error), accept the failure immediately. Do NOT
    retry the same URL under any action. Do NOT navigate to it. Move on.

14. CRAWL TYPE HINT: You will be told the crawl type of the site (static, dynamic, or api).
    - 'static' : pagination uses standard <a> links — prefer action=navigate.
    - 'dynamic': content is JS-rendered — prefer action=navigate but expect slight delays.
    - 'api'    : pagination is JS-triggered (buttons/onclick) — prefer action=click over
                 action=navigate for Next page links, using the selector provided.

15. DUPLICATE PUBLICATIONS: A listing page may show the same publication under two different
    entry titles (e.g. a news announcement AND a report entry for the same document). These
    are DIFFERENT listing entries and must each be visited. Do NOT assume a listing entry has
    already been handled just because a similarly-named PDF was downloaded. Always visit the
    detail page and check for a unique PDF URL before skipping.

16. NEVER attempt to guess or construct a PDF URL from a publication title on a listing
    page. You MUST first navigate to the publication's detail page, then find the PDF
    link that actually appears in the links list on that detail page. A listing page
    title like "SCO - Scope and definitions" does NOT tell you the PDF URL — only the
    detail page does.

17. DATE FILTERS: Some listing pages have search/filter inputs like "Published After",
    "Published Before", "Date from", "Date to", or similar. If you see these on a
    listing page, USE THEM BEFORE paginating — fill in the target date range to
    pre-filter results. Use action=fill with the CSS selector of the input and the
    date value in the format the field expects (try yyyy-mm-dd first). After filling
    both fields, submit the form using action=click on the search/submit button.
    This is always preferred over paginating through unfiltered results.

18. MULTILINGUAL PDF GRIDS: Some detail pages show a grid of PDF links organized by
    language and format rather than a simple "Download PDF" button. The grid may have
    rows representing different formats and columns for different languages.
    To download the English PDF from such a grid:
    a. Find the row labelled "PDF" (or the primary PDF format)
    b. Find the column labelled "EN" (English) or similar language indicator
    c. The cell at that intersection contains the PDF link — use action=download_pdf
       with that link's href.
    d. If you cannot identify the exact cell, look in the links list for any link whose
       href contains "/EN/" or ends in ".pdf" and whose surrounding text or
       title attribute mentions "EN" or "English".
    e. Never use action=fill on language/format selector dropdowns — these may render
       HTML viewers or alternative formats, not downloadable PDFs."""

def _unambiguous_target(target_month: str) -> str:
    """Reformat target_month into an unambiguous English string for the LLM.

    Converts numeric dates like '1/3/2026' (ambiguous MM/DD vs DD/MM) into
    'after 1 March 2026' so the model cannot misinterpret them.
    """
    s = target_month.strip()
    for prefix in ("after ", "from "):
        if s.lower().startswith(prefix):
            try:
                dt = _parse_target_range(s)[0]
                return f"{prefix.strip()} {dt.day} {dt.strftime('%B %Y')}"
            except Exception:
                return s
    if s.lower().startswith("before "):
        try:
            dt = _parse_target_range(s)[1]
            return f"before {dt.day} {dt.strftime('%B %Y')}"
        except Exception:
            return s
    if " to " in s:
        try:
            start_dt, end_dt = _parse_target_range(s)
            return f"{start_dt.day} {start_dt.strftime('%B %Y')} to {end_dt.day} {end_dt.strftime('%B %Y')}"
        except Exception:
            return s
    try:
        start_dt, _ = _parse_target_range(s)
        return start_dt.strftime("%B %Y")
    except Exception:
        return s


def call_llm(source, page_state, history, failed_urls=None, downloaded_urls=None, visited_urls=None, target_month=None, crawl_type: str = "unknown"):
    target_month = _unambiguous_target(target_month or TARGET_MONTH)
    """Send current page state + conversation history to the LLM; return parsed action."""
    notes = ""
    if failed_urls:
        notes += (
            f"\n\nAlready-failed PDF URLs (do NOT attempt these again):\n"
            + "\n".join(f"  - {u}" for u in failed_urls)
        )
    if downloaded_urls:
        notes += (
            f"\n\nAlready-downloaded PDF URLs (do NOT attempt these again):\n"
            + "\n".join(f"  - {u}" for u in downloaded_urls)
        )
    if visited_urls:
        notes += (
            f"\n\nAlready-visited detail page URLs (do NOT navigate to these again):\n"
            + "\n".join(f"  - {u}" for u in visited_urls)
        )
    user_msg = (
        f"Organisation: {source['Organization']}\n"
        f"  Source URL : {source['OfficialSourceForPDFs']}\n"
        f"  Crawl type : {crawl_type}\n"
        f"Goal: find and download ALL PDF publications released in {target_month} from {source['OfficialSourceForPDFs']}\n"
        + notes
        + f"\n\nCurrent page\n"
        f"  URL        : {page_state['url']}\n"
        f"  Title      : {page_state['title']}\n"
        f"  Body text  :\n{page_state['body_text']}\n\n"
        + (
            f"  NEXT PAGE URL (use this exact URL with action=navigate to paginate): {page_state['pagination']['next']}\n"
            if page_state.get('pagination', {}).get('next')
            else "  NEXT PAGE URL: not found on this page\n"
        )
        + (
            f"  NEXT BUTTON SELECTOR (use action=click with this selector to paginate): {page_state['pagination']['next_button_selector']}\n"
            if page_state.get('pagination', {}).get('next_button_selector')
            else "  NEXT BUTTON SELECTOR: not found\n"
        )
        + f"  Links on page (text \u2192 href):\n"
        + "\n".join(
            f"    {l['text']!r} [title={l.get('title', '')!r}] → {l['href']}"
            for l in page_state["links"]
        )
        + "\n\nWhat is the next action?"
    )

    history.append({"role": "user", "content": user_msg})

    # Keep only the last 6 messages (3 turns) to prevent history from bloating
    # the request — older full page states add no value and cause API timeouts.
    trimmed_history = history[-6:]

    llm_model = os.getenv("LLM_MODEL", "google/gemini-2.5-flash")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": llm_model,
            "messages": [{"role": "system", "content": build_system_prompt(target_month)}] + trimmed_history,
        },
        timeout=60,
    )
    if not response.ok:
        if response.status_code == 402:
            raise RuntimeError(
                "OpenRouter API returned 402 Payment Required — "
                "please add credits at https://openrouter.ai/credits"
            )
        response.raise_for_status()

    resp_json = response.json()
    reply_text = resp_json["choices"][0]["message"]["content"]
    usage = resp_json.get("usage", {})
    if usage:
        print(f"  [tokens] prompt={usage.get('prompt_tokens', '?')}  completion={usage.get('completion_tokens', '?')}  total={usage.get('total_tokens', '?')}")
    history.append({"role": "assistant", "content": reply_text})

    def _parse_json(text: str):
        """Strip markdown fences and parse JSON; raise JSONDecodeError on failure."""
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1]
            clean = clean.split("```", 1)[0]
            if clean.startswith("json"):
                clean = clean[4:]
        return json.loads(clean.strip())

    try:
        return _parse_json(reply_text)
    except json.JSONDecodeError:
        # LLM returned malformed / truncated JSON — ask it to resend
        print(f"  ⚠ LLM returned invalid JSON, retrying once...")
        history.append({"role": "user", "content": (
            "Your last response was not valid JSON. "
            "Reply with ONLY a single valid JSON action object — no extra text, no markdown fences."
        )})
        retry_resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": llm_model,
                "messages": [{"role": "system", "content": build_system_prompt(target_month)}]
                             + history[-10:],
            },
            timeout=60,
        )
        retry_resp.raise_for_status()
        retry_text = retry_resp.json()["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": retry_text})
        return _parse_json(retry_text)


# ── Core scraper ───────────────────────────────────────────────────────────────

def scrape_source(source, page, max_steps=100):
    print(f"\n{'='*60}")
    print(f"Scraping : {source['Organization']}")
    print(f"URL      : {source['OfficialSourceForPDFs']}")

    page.goto(source["OfficialSourceForPDFs"], wait_until="domcontentloaded", timeout=60000)

    crawl_type = detect_crawl_type(page)
    print(f"  Crawl type detected: {crawl_type}")

    history = []         # running conversation with the LLM
    downloaded = []      # filenames of PDFs saved so far
    downloaded_urls = [] # PDF URLs successfully downloaded — passed to LLM to avoid re-downloads
    failed_urls = []     # PDF URLs that already failed — passed to LLM to avoid retries
    visited_urls = []    # detail page URLs already visited — passed to LLM to avoid revisits
    listing_urls = set() # URLs that are listing/pagination pages (never block these)
    listing_urls.add(source["OfficialSourceForPDFs"].rstrip("/"))
    listing_urls.add(source["OfficialSourceForPDFs"].rstrip("/") + "/")

    # ── Auto-navigate to publications page if the landing page is not a listing ──
    pub_url = _find_publications_url(page)
    if pub_url:
        print(f"  ↳ Landing page is not a publications listing — auto-navigating to: {pub_url}")
        page.goto(pub_url, wait_until="domcontentloaded", timeout=60000)
        listing_urls.add(pub_url.rstrip("/"))
        listing_urls.add(pub_url.rstrip("/") + "/")
        crawl_type = detect_crawl_type(page)  # re-detect after navigation
        print(f"  Crawl type re-detected: {crawl_type}")

    # ── Apply date filters before the agentic loop ────────────────────────────
    # This is done deterministically — more reliable than asking the LLM to do it.
    # Track which URLs have already had filters applied so we can re-run when
    # the LLM navigates to a different/correct listing page later.
    filtered_listing_urls: set = set()
    if _apply_date_filters(page, TARGET_MONTH):
        filtered_listing_urls.add(page.url.rstrip("/"))

    step = 0
    before_target_override_count = 0  # Fix 1: cap the before_target safety override
    fabrication_strikes = 0            # Fix 3: count LLM URL fabrications
    items_remaining_override_count = 0 # Fix 2: cap the items-remaining override

    # ── Agentic loop ──────────────────────────────────────────────────────────
    # Each iteration:
    #   1. Capture current page state with Playwright
    #   2. Send state + history to the LLM
    #   3. LLM decides the next action (or signals done)
    #   4. Playwright executes the action
    # The LLM controls termination via {"action": "done"}.
    # max_steps is a safety guardrail in case the LLM never signals done.

    last_action_key = None
    repeat_count = 0

    while step < max_steps:
        step += 1
        print(f"\n  [step {step}] capturing page state...")

        # Bail out immediately if the browser window was closed
        if page.is_closed():
            print(f"  ⚠ Browser page is closed — stopping.")
            break

        try:
            page_state = get_page_state(page)
        except Exception as _e:
            if "closed" in str(_e).lower():
                print(f"  ⚠ Browser was closed while capturing page state — stopping.")
                break
            raise

        action = call_llm(source, page_state, history, failed_urls, downloaded_urls, visited_urls, crawl_type=crawl_type)

        print(f"  [step {step}] action={action['action']}  reason={action.get('reason', '')}")

        # Detect repeated identical actions (e.g. stuck on Cloudflare challenge)
        action_key = (action["action"], action.get("url"), action.get("selector"))
        if action_key == last_action_key:
            repeat_count += 1
            if repeat_count >= 3:
                print(f"  ⚠ Same action repeated {repeat_count} times — stopping to avoid infinite loop.")
                break
        else:
            last_action_key = action_key
            repeat_count = 0

        if action["action"] == "done":
            # Safety override: check if the current page still contains unvisited TARGET_MONTH items
            try:
                _range_start, _range_end = _parse_target_range(TARGET_MONTH)
                body_lower = page_state["body_text"].lower()
                # Check for any day-level date references for any month in the target range
                has_target_dates = False
                if "publication date" in body_lower:
                    _check = _range_start
                    while (_check.year, _check.month) <= (_range_end.year, _range_end.month):
                        month_abbr = _check.strftime("%b")
                        month_full = _check.strftime("%B")
                        year_str   = _check.strftime("%Y")
                        if (
                            f"{month_abbr.lower()} {year_str}" in body_lower
                            or f"{month_full.lower()} {year_str}" in body_lower
                        ):
                            has_target_dates = True
                            break
                        if _check.month == 12:
                            _check = _check.replace(year=_check.year + 1, month=1)
                        else:
                            _check = _check.replace(month=_check.month + 1)
                # Confirm there are links on the page not yet visited/downloaded
                page_link_urls = {l["href"] for l in page_state["links"]}
                has_unvisited = bool(
                    page_link_urls - set(visited_urls) - set(downloaded_urls) - listing_urls
                    - {page_state["url"]}
                )
                if has_target_dates and has_unvisited and items_remaining_override_count < 2:
                    items_remaining_override_count += 1
                    print(f"  ⚠ LLM signalled done but {TARGET_MONTH} content appears to still be on this page. Overriding ({items_remaining_override_count}/2).")
                    history.append({"role": "user", "content": (
                        f"OVERRIDE: You called done but the current page body still contains references to "
                        f"{TARGET_MONTH} publications that have not been downloaded yet. "
                        f"You MUST NOT call done. Process every {TARGET_MONTH} item on this page before "
                        f"paginating or stopping."
                    )})
                    last_action_key = None
                    repeat_count = 0
                    continue
            except Exception:
                pass  # if date parsing fails, fall through to normal checks

            # Safety override: if there's a next page we haven't visited yet,
            # AND we haven't scrolled past the target month yet, keep going.
            next_url = page_state.get("pagination", {}).get("next")
            _link_texts = " ".join(l["text"] for l in page_state.get("links", []))
            already_past = _page_is_past_target(page_state["body_text"], TARGET_MONTH, _link_texts)
            before_target = _page_is_before_target(page_state["body_text"], TARGET_MONTH, _link_texts)
            if next_url and next_url not in visited_urls and next_url not in listing_urls and not already_past:
                print(f"  ⚠ LLM signalled done but a Next page exists and target month not yet past. Overriding — navigating to {next_url}.")
                history.append({"role": "user", "content": (
                    f"OVERRIDE: You called done, but there is a Next page available at {next_url} "
                    f"that has not been visited yet, and the current page has not scrolled past {TARGET_MONTH}. "
                    f"You MUST NOT stop here. Navigate to {next_url} now and continue searching for {TARGET_MONTH} publications."
                )})
                page.goto(next_url, wait_until="domcontentloaded", timeout=60000)
                listing_urls.add(next_url)
                last_action_key = None
                repeat_count = 0
                continue
            elif before_target and not already_past:
                # The JS extractor didn't find the next URL, but we know the target month
                # is on a later page because this page shows only NEWER content.
                before_target_override_count += 1
                if before_target_override_count >= 3:
                    print(f"  ⚠ before_target override fired {before_target_override_count} times with no progress — accepting done.")
                    break
                print(f"  ⚠ LLM signalled done but page shows only NEWER content — target month is ahead. Prompting LLM to paginate.")
                history.append({"role": "user", "content": (
                    f"OVERRIDE: You called done, but the current page shows ONLY content NEWER than {TARGET_MONTH}. "
                    f"Because publications are sorted newest-first, {TARGET_MONTH} content is on a LATER page. "
                    f"You MUST keep paginating. "
                    f"Look in the links list for a link with text 'Next', 'Next page', '>', '›', '»', or a "
                    f"page number, and navigate to it immediately. Do NOT call done."
                )})
                last_action_key = None
                repeat_count = 0
                continue
            print(f"  → LLM signalled done. Downloaded {len(downloaded)} PDF(s).")
            break

        elif action["action"] == "navigate":
            nav_url = action["url"]
            # Guard: if current page content is already past the target month,
            # the LLM should not navigate forward — there's nothing newer to find.
            # Exception: allow navigating back to a known listing/source URL.
            _nav_link_texts = " ".join(l["text"] for l in page_state.get("links", []))
            _nav_already_past = _page_is_past_target(page_state["body_text"], TARGET_MONTH, _nav_link_texts)
            _nav_to_listing = nav_url.rstrip("/") in {u.rstrip("/") for u in listing_urls}
            if _nav_already_past and not _nav_to_listing:
                print(f"  ⚠ Current page content is past target month but LLM tried to navigate further. Forcing done.")
                history.append({"role": "user", "content": (
                    f"OVERRIDE: The current page shows ONLY content OLDER than {TARGET_MONTH}. "
                    f"Navigating to further pages will only show even older content — there is nothing "
                    f"more to find. You MUST call action=done now."
                )})
                last_action_key = None
                repeat_count = 0
                continue
            # Guard: block fabricated URLs not present in the current page's links.
            # Only treat a URL as fabricated if it goes to a DIFFERENT domain.
            # Same-domain URLs — even with pagination params not seen in the links list —
            # are allowed: dynamic sites often build next-page URLs in JS so they never
            # appear in the static links snapshot.
            known_urls = {l["href"] for l in page_state["links"]}
            known_urls |= listing_urls
            known_urls.add(page_state["url"])
            known_urls.add(source["OfficialSourceForPDFs"].rstrip("/"))
            known_urls.add(source["OfficialSourceForPDFs"].rstrip("/") + "/")
            from urllib.parse import urlparse, parse_qs
            nav_parsed = urlparse(nav_url)
            source_parsed = urlparse(source["OfficialSourceForPDFs"])
            current_parsed = urlparse(page.url)
            same_domain = (
                nav_parsed.netloc == source_parsed.netloc
                or nav_parsed.netloc == current_parsed.netloc
            )
            is_fabricated = not same_domain
            if is_fabricated:
                fabrication_strikes += 1
                if fabrication_strikes >= 3:
                    print(f"  ⚠ LLM fabricated URLs {fabrication_strikes} times — no real Next link exists. Stopping.")
                    break
                err_msg = (
                    f"BLOCKED: {nav_url} does not appear in the links list — this URL was fabricated. "
                    f"Rule 9 forbids fabricating URLs. You MUST only use URLs that appear exactly in the "
                    f"provided links list. Look again at the links list for a real 'Next page' link, "
                    f"or call done if none exists."
                )
                print(f"  ⚠ fabricated URL blocked: {nav_url}")
                history.append({"role": "user", "content": err_msg})
                last_action_key = None
                repeat_count = 0
                continue
            # Guard: never navigate to a known-failed URL
            if nav_url in failed_urls:
                err_msg = (
                    f"BLOCKED: {nav_url} is in the failed list. "
                    f"Do NOT attempt this URL again in any action. Move on."
                )
                print(f"  ⚠ navigate to failed URL blocked.")
                history.append({"role": "user", "content": err_msg})
                last_action_key = None
                repeat_count = 0
            # Guard: never revisit an already-visited detail page
            elif nav_url in visited_urls:
                err_msg = (
                    f"BLOCKED: {nav_url} was already visited and had no PDF. "
                    f"Do NOT navigate here again. Move on to the next item."
                )
                print(f"  ⚠ navigate to already-visited page blocked.")
                history.append({"role": "user", "content": err_msg})
                last_action_key = None
                repeat_count = 0
            # Guard: if the LLM tries to navigate directly to a PDF, download it instead
            elif nav_url.lower().endswith(".pdf") or "/content/dam/" in nav_url.lower():
                print(f"  ⚠ navigate to PDF URL detected — routing through download logic.")
                action["action"] = "download_pdf"
                action["filename"] = nav_url.rstrip("/").split("/")[-1] or f"doc_{step}.pdf"
                # fall through to download_pdf branch below by re-processing
                pdf_url = nav_url
                filename = action["filename"]
                try:
                    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                    headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        "Referer": page.url,
                    }
                    dl_response = requests.get(pdf_url, cookies=cookies, headers=headers, timeout=60)
                    dl_response.raise_for_status()
                    content = dl_response.content
                    if not content.startswith(b"%PDF"):
                        raise ValueError(
                            f"Response is not a PDF (content-type: {dl_response.headers.get('content-type', 'unknown')})"
                        )
                    filepath = os.path.join(PDF_OUTPUT_DIR, filename)
                    with open(filepath, "wb") as fh:
                        fh.write(content)
                    downloaded.append(filepath)
                    downloaded_urls.append(pdf_url)
                    print(f"  → Saved: pdfs_downloaded/{filename}")
                except Exception as e:
                    failed_urls.append(pdf_url)
                    err_msg = (
                        f"ACTION FAILED: PDF download failed for {pdf_url}: {e}. "
                        f"This URL does NOT work — do NOT retry it via navigate or download_pdf. "
                        f"Accept the failure and move on to the next publication."
                    )
                    print(f"  ⚠ download (via navigate) failed ({e}), notifying LLM.")
                    history.append({"role": "user", "content": err_msg})
                    last_action_key = None
                    repeat_count = 0
            else:
                try:
                    page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as _nav_e:
                    if "closed" in str(_nav_e).lower():
                        print(f"  ⚠ Browser was closed during navigation — stopping.")
                        return downloaded
                    raise
                fabrication_strikes = 0            # reset on successful navigation
                before_target_override_count = 0  # reset override counter on real navigation
                items_remaining_override_count = 0  # reset on real navigation to new page
                # Crawl-type-aware post-navigation wait
                if crawl_type == "dynamic":
                    page.wait_for_timeout(1000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                elif crawl_type == "api":
                    page.wait_for_timeout(1000)
                    try:
                        page.wait_for_selector(
                            "article, main, [class*='result'], [class*='item']",
                            timeout=5000,
                        )
                    except Exception:
                        pass
                # Detect if this is a listing/pagination page by checking for
                # pagination indicators in the loaded page
                is_listing = (
                    nav_url in listing_urls
                    or nav_url.rstrip("/") == source["OfficialSourceForPDFs"].rstrip("/")
                )
                if not is_listing:
                    # Check if the page we landed on looks like a listing page
                    # (has pagination or multiple publication items)
                    has_pagination = page.evaluate("""
                        () => {
                            const text = document.body ? document.body.innerText : '';
                            return text.includes('Next page') || text.includes('Showing results') || text.includes('Previous page');
                        }
                    """)
                    if has_pagination:
                        listing_urls.add(nav_url)
                        is_listing = True
                _was_in_visited = nav_url in visited_urls
                if not is_listing and not _was_in_visited:
                    visited_urls.append(nav_url)
                # Re-apply date filters on any page not yet filtered and not
                # previously classified as a detail page.
                # NOTE: must use _was_in_visited (captured BEFORE appending above)
                # because appending would make `nav_url not in visited_urls` False.
                if not _was_in_visited and nav_url.rstrip("/") not in filtered_listing_urls:
                    if _apply_date_filters(page, TARGET_MONTH):
                        filtered_listing_urls.add(nav_url.rstrip("/"))
                        crawl_type = detect_crawl_type(page)

        elif action["action"] == "click":
            import re as _re
            selector = action["selector"]
            # If the selector targets an anchor, navigate directly instead of clicking
            # (avoids Playwright timeout when <a> elements aren't interactable)
            href_match = _re.search(r"""a\[href=['"]([^'"]+)['"]\]""", selector)
            if not href_match:
                # Also resolve any <a> href via JS before attempting a real click
                try:
                    resolved_href = page.evaluate(
                        """(sel) => {
                            const el = document.querySelector(sel);
                            if (!el) return null;
                            const a = el.tagName === 'A' ? el : el.querySelector('a');
                            return a ? a.href : null;
                        }""",
                        selector,
                    )
                    if resolved_href and not resolved_href.startswith("javascript:"):
                        href_match = type('_M', (), {'group': lambda self, n: resolved_href})()
                except Exception:
                    pass
            if href_match:
                page.goto(href_match.group(1), wait_until="domcontentloaded", timeout=60000)
            else:
                try:
                    # Capture results text before clicking so we can detect when the page actually updates
                    try:
                        pre_click_results_text = page.evaluate("() => document.body.innerText")
                    except Exception:
                        pre_click_results_text = ""
                    page.click(selector, timeout=15000)
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(800)  # let JS render new results after button click
                    # Poll up to 5 more seconds for the results to actually change
                    try:
                        import time as _time
                        for _ in range(5):
                            new_text = page.evaluate("() => document.body.innerText")
                            if new_text != pre_click_results_text:
                                break
                            _time.sleep(1)
                    except Exception:
                        pass
                    items_remaining_override_count = 0
                    before_target_override_count = 0
                except Exception as e:
                    # click failed — try to resolve the element's href via JS and navigate
                    try:
                        href = page.evaluate(
                            """(sel) => {
                                const el = document.querySelector(sel);
                                if (!el) return null;
                                const a = el.tagName === 'A' ? el : el.querySelector('a');
                                return a ? a.href : null;
                            }""",
                            selector,
                        )
                        if href:
                            print(f"  ⚠ click failed, resolved href and navigating instead.")
                            page.goto(href, wait_until="domcontentloaded", timeout=60000)
                            last_action_key = None
                            repeat_count = 0
                        else:
                            raise RuntimeError("no href found on element")
                    except Exception:
                        err_msg = (
                            f"ACTION FAILED: click on '{selector}' failed and no href could be resolved. "
                            f"Do NOT retry the same selector and do NOT call done. "
                            f"You MUST use action=navigate with the NEXT PAGE URL shown above the links list "
                            f"to continue paginating. If no Next Page URL was shown, look in the links list "
                            f"for any link with text 'Next page', 'Next', or '>' and navigate to it."
                        )
                        print(f"  ⚠ click failed, notifying LLM.")
                        history.append({"role": "user", "content": err_msg})
                        last_action_key = None
                        repeat_count = 0

        elif action["action"] == "download_pdf":
            pdf_url = action["url"]
            # Strip Adobe CQ5/AEM download suffixes the LLM sometimes appends
            for suffix in (".coredownload.inline.pdf", ".coredownload.pdf", ".coredownload"):
                if pdf_url.lower().endswith(suffix):
                    pdf_url = pdf_url[: -len(suffix)]
                    print(f"  ⚠ stripped fabricated suffix, using: {pdf_url}")
                    break
            filename = action.get("filename", f"doc_{step}.pdf")
            # Guard: block aggregate/full-version PDF downloads
            if any(kw in filename.lower() or kw in pdf_url.lower() for kw in AGGREGATE_PDF_KEYWORDS):
                print(f"  ⚠ Skipping aggregate/full-version PDF: {pdf_url}")
                history.append({"role": "user", "content": (
                    f"BLOCKED: {pdf_url} appears to be a site-wide aggregate document, not an "
                    f"individual publication. Do NOT download it. Return to the listing page."
                )})
                last_action_key = None
                repeat_count = 0
                continue
            # Guard: verify the publication date on the current detail page is within the
            # target range before downloading. This catches cases where a site's date
            # filter returns slightly out-of-range items.
            try:
                _range_start, _range_end = _parse_target_range(TARGET_MONTH)
                _body = page_state["body_text"]
                # Extract all dates from the page body and check if any fall in range
                import re as _re2
                # Match common date formats: "27 Nov 2024", "27/11/2024", "2024-11-27", "November 27, 2024"
                _date_patterns = [
                    (r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{4})\b', '%d %b %Y'),
                    (r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b', '%b %d %Y'),
                    (r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', None),
                    (r'\b(\d{4})-(\d{2})-(\d{2})\b', None),
                ]
                _found_dates = []
                for pat, fmt in _date_patterns:
                    for m in _re2.finditer(pat, _body, _re2.IGNORECASE):
                        try:
                            if fmt:
                                g = m.groups()
                                if len(g) == 3:
                                    raw = f"{g[0]} {g[1]} {g[2]}"
                                    _found_dates.append(datetime.datetime.strptime(raw, fmt))
                            else:
                                g = m.groups()
                                if len(g) == 3:
                                    # Try dd/mm/yyyy then mm/dd/yyyy, then yyyy-mm-dd
                                    for candidate_fmt in ('%d/%m/%Y', '%m/%d/%Y'):
                                        try:
                                            _found_dates.append(datetime.datetime.strptime(f"{g[0]}/{g[1]}/{g[2]}", candidate_fmt))
                                            break
                                        except ValueError:
                                            pass
                                    # yyyy-mm-dd
                                    try:
                                        _found_dates.append(datetime.datetime.strptime(f"{g[0]}-{g[1]}-{g[2]}", '%Y-%m-%d'))
                                    except ValueError:
                                        pass
                        except (ValueError, IndexError):
                            pass
                # Filter out dates that are clearly spurious (e.g. historical dates
                # from years/decades ago that may appear in document metadata or references).
                # Keep only dates within 5 years before or after the target range.
                _date_window_start = _range_start - datetime.timedelta(days=5 * 365)
                _found_dates = [d for d in _found_dates if d >= _date_window_start]
                _in_range = [d for d in _found_dates if _range_start <= d <= _range_end]
                _out_of_range = [d for d in _found_dates if d < _range_start or d > _range_end]
                if _found_dates and not _in_range:
                    _date_strs = [d.strftime('%d %b %Y') for d in _out_of_range[:3]]
                    print(f"  ⚠ Date check: publication date(s) {_date_strs} are OUTSIDE target range — skipping download.")
                    history.append({"role": "user", "content": (
                        f"BLOCKED: The publication date(s) on this page ({', '.join(_date_strs)}) are outside "
                        f"the target range. Do NOT download this PDF. Return to the listing page."
                    )})
                    last_action_key = None
                    repeat_count = 0
                    continue
                elif _in_range:
                    print(f"  ↳ Date check: publication date confirmed in range ({_in_range[0].strftime('%d %b %Y')})")
            except Exception:
                pass  # date check is best-effort; don't block downloads on parse failures
            try:
                # Extract cookies from the live Playwright session so authenticated
                # downloads work, then use requests for reliable binary transfer.
                cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Referer": page.url,
                }
                dl_response = requests.get(pdf_url, cookies=cookies, headers=headers, timeout=60)
                dl_response.raise_for_status()
                content = dl_response.content
                # Validate magic bytes — real PDFs start with %PDF
                if not content.startswith(b"%PDF"):
                    raise ValueError(
                        f"Response is not a PDF (content-type: {dl_response.headers.get('content-type', 'unknown')})"
                    )
                filepath = os.path.join(PDF_OUTPUT_DIR, filename)
                with open(filepath, "wb") as fh:
                    fh.write(content)
                downloaded.append(filepath)
                downloaded_urls.append(pdf_url)
                print(f"  → Saved: pdfs_downloaded/{filename}")
            except Exception as e:
                if "Response is not a PDF" in str(e):
                    # Fallback: URL is a JS-triggered download, use Playwright click-to-download
                    print(f"  ⚠ requests returned non-PDF content — trying Playwright browser download.")
                    try:
                        filepath = os.path.join(PDF_OUTPUT_DIR, filename)
                        with page.expect_download(timeout=30000) as dl_info:
                            page.goto(pdf_url)
                        dl = dl_info.value
                        dl.save_as(filepath)
                        with open(filepath, "rb") as f:
                            if not f.read(4).startswith(b"%PDF"):
                                raise ValueError("Downloaded file is not a PDF")
                        downloaded.append(filepath)
                        downloaded_urls.append(pdf_url)
                        print(f"  → Saved via browser download: pdfs_downloaded/{filename}")
                    except Exception as e2:
                        failed_urls.append(pdf_url)
                        history.append({"role": "user", "content": (
                            f"ACTION FAILED: JS download also failed for {pdf_url}: {e2}. "
                            f"Accept and move on."
                        )})
                        print(f"  ⚠ browser download also failed ({e2}), notifying LLM.")
                        last_action_key = None
                        repeat_count = 0
                else:
                    failed_urls.append(pdf_url)
                    err_msg = (
                        f"ACTION FAILED: download_pdf raised an error for {pdf_url}: {e}. "
                        f"This URL does NOT work — do NOT retry it via navigate or any other action. "
                        f"Accept the failure and move on to the next publication."
                    )
                    print(f"  ⚠ download failed ({e}), notifying LLM.")
                    history.append({"role": "user", "content": err_msg})
                    last_action_key = None
                    repeat_count = 0

        elif action["action"] == "fill":
            selector = action["selector"]
            value = action.get("value", "")
            try:
                page.fill(selector, value, timeout=10000)
                print(f"  → Filled '{selector}' with '{value}'")
            except Exception as e:
                print(f"  ⚠ fill failed ({e}), notifying LLM.")
                history.append({"role": "user", "content": f"ACTION FAILED: fill on '{selector}' with '{value}' failed: {e}. Try a different selector or skip filtering."})
                last_action_key = None
                repeat_count = 0

        else:
            print(f"  [step {step}] unknown action '{action['action']}', stopping.")
            break

    else:
        print(f"  ⚠ Reached max_steps ({max_steps}) without LLM signalling done.")

    return downloaded

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # visible window helps bypass Cloudflare bot detection
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
        )
        page = context.new_page()

        all_downloaded = []
        for source in sources:
            results = scrape_source(source, page)
            all_downloaded.extend(results)

        context.close()
        browser.close()

        print(f"\n✅ Done — {len(all_downloaded)} PDF(s) downloaded total.")  
