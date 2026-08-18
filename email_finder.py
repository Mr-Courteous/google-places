"""
email_finder.py
----------------
Given a business website URL, tries to find a contact email AND phone
number published on that site (homepage + common contact-page paths).
Does NOT touch Google Maps or Google's own pages — only the business's
own website, which they control and have chosen to publish.

Two extraction passes:
1. Plain HTTP fetch + regex (fast, works for most server-rendered sites).
2. If that finds nothing, and Playwright is installed, render the page
   in a real headless browser and try again. This catches sites that
   build their footer/contact info with JavaScript (common with
   WordPress page-builders like Elementor, and any React/Next/Vue site),
   which a plain HTTP fetch never sees because no JS ever runs.

Playwright is OPTIONAL. If it's not installed, step 2 is silently
skipped and step 1's result (possibly None) is used. To enable it:
    pip install playwright
    playwright install chromium

Usage as a library:
    from email_finder import find_email, find_contact_info
    email = find_email("https://example.com")
    info = find_contact_info("https://example.com")
    # info == {"emails": [...], "phones": [...]}
"""

import re
import html
import requests

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
MAILTO_RE = re.compile(r'href=["\']mailto:([^"\'?]+)', re.IGNORECASE)

# Catches "info [at] domain [dot] com", "info(at)domain(dot)com",
# "info at domain dot com" style obfuscation some sites use.
OBFUSCATED_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*[\[\(]?\s*at\s*[\]\)]?\s*[a-zA-Z0-9.\-]+\s*[\[\(]?\s*dot\s*[\]\)]?\s*[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Nigerian-friendly phone matcher: +234 xxx, 0xxx local, with common
# separators. Deliberately a bit loose since site formatting varies a lot.
PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s\-.]?)?(?:\(?0\)?[\s\-.]?)?\d{2,4}[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}"
)

CONTACT_PATHS = ["", "/contact", "/contact-us", "/contactus", "/about", "/about-us"]

JUNK_SUBSTRINGS = [
    "wixpress.com", "sentry.io", "godaddy.com", "example.com",
    "yourname@", "email@", "domain.com", "@2x", ".png", ".jpg",
    ".jpeg", ".gif", ".svg", ".webp", "schema.org", "w3.org",
    "sentry-next", "wp.com",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_PLAYWRIGHT_AVAILABLE = None  # cached probe result


def _is_junk(email: str) -> bool:
    lower = email.lower()
    return any(junk in lower for junk in JUNK_SUBSTRINGS)


def _deobfuscate(text: str) -> str:
    """Turn 'info [at] domain [dot] com' style text into a real address."""
    text = re.sub(r"\s*[\[\(]?\s*at\s*[\]\)]?\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[\[\(]?\s*dot\s*[\]\)]?\s*", ".", text, flags=re.IGNORECASE)
    return text.replace(" ", "")


def _extract_emails(raw_html: str):
    text = html.unescape(raw_html)
    candidates = []

    for m in MAILTO_RE.findall(text):
        addr = m.strip()
        if addr and not _is_junk(addr):
            candidates.append(addr)

    for m in EMAIL_RE.findall(text):
        if not _is_junk(m):
            candidates.append(m)

    for m in OBFUSCATED_RE.findall(text):
        deobf = _deobfuscate(m)
        if EMAIL_RE.match(deobf) and not _is_junk(deobf):
            candidates.append(deobf)

    seen = set()
    out = []
    for c in candidates:
        c_clean = c.strip().rstrip(".,;")
        if c_clean.lower() not in seen:
            seen.add(c_clean.lower())
            out.append(c_clean)
    return out


def _extract_phones(raw_html: str):
    # Strip tags roughly so we're matching visible-ish text, not markup/IDs
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw_html))
    candidates = []
    for m in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", m)
        if 7 <= len(digits) <= 14:
            candidates.append(m.strip())

    seen = set()
    out = []
    for c in candidates:
        key = re.sub(r"\D", "", c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _fetch_plain(url: str, timeout: int = 8):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp.text
    except requests.RequestException:
        pass
    return None


def _playwright_available() -> bool:
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            import playwright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


def _fetch_rendered(url: str, timeout_ms: int = 12000):
    """Render the page with a real (headless) browser to catch JS-built content."""
    if not _playwright_available():
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            content = page.content()
            browser.close()
            return content
    except Exception:
        return None


def find_contact_info(website_url: str, timeout: int = 8, use_js_fallback: bool = True):
    """
    Visit the homepage and a few common contact-page paths. Returns
    {"emails": [...], "phones": [...]} — both lists may be empty.
    """
    if not website_url:
        return {"emails": [], "phones": []}

    base = website_url.rstrip("/")
    all_emails = []
    all_phones = []

    for path in CONTACT_PATHS:
        url = base + path
        raw = _fetch_plain(url, timeout=timeout)

        if raw is None:
            continue

        emails = _extract_emails(raw)
        phones = _extract_phones(raw)

        # If a plain fetch found nothing at all on the homepage, the page
        # is a likely JS-rendered SPA/widget — try a real browser render.
        if not emails and not phones and path == "" and use_js_fallback:
            rendered = _fetch_rendered(url)
            if rendered:
                emails = _extract_emails(rendered)
                phones = _extract_phones(rendered)

        all_emails.extend(emails)
        all_phones.extend(phones)

        if all_emails:
            break  # good enough, stop checking further pages

    # de-dupe while preserving order
    def dedupe(items, key=lambda x: x.lower()):
        seen = set()
        out = []
        for i in items:
            k = key(i)
            if k not in seen:
                seen.add(k)
                out.append(i)
        return out

    return {
        "emails": dedupe(all_emails),
        "phones": dedupe(all_phones, key=lambda x: re.sub(r"\D", "", x)),
    }


def find_email(website_url: str, timeout: int = 8):
    """Backward-compatible helper: returns the single best email, or None."""
    info = find_contact_info(website_url, timeout=timeout)
    return info["emails"][0] if info["emails"] else None


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python email_finder.py <website_url>")
        sys.exit(1)
    result = find_contact_info(sys.argv[1])
    print("Emails:", result["emails"] or "none found")
    print("Phones:", result["phones"] or "none found")
