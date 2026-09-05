"""
Google Places Lead Finder — Web UI
------------------------------------
A small local web app: enter a business type + city, click search,
get a table of leads (name, phone, address, website) with a CSV download.

SETUP:
1. pip install flask requests python-dotenv
2. Edit the .env file in this folder and replace 'your_key_here' with your
   real Google Places API key (get one at console.cloud.google.com,
   enable "Places API (New)")
3. Run:  python app.py
4. Open in your browser:  http://127.0.0.1:5000

Your API key stays on your machine — this app is NOT deployed publicly,
so it's never exposed to anyone else.
"""

import os
import csv
import io
import re
import time
import hmac
import base64
import hashlib
import difflib
import threading
import concurrent.futures
import requests
from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv

from email_finder import find_contact_info
from email_verifier import verify_email, check_port_25
from lead_file_parser import parse_upload

# Explicitly point at the .env file next to this script, so it loads
# correctly no matter what directory you launch the app from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)

RESEND_WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "")
SUPPRESSION_PATH = os.path.join(BASE_DIR, "suppression.csv")

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

SEARCH_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,nextPageToken"
DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,internationalPhoneNumber,"
    "nationalPhoneNumber,websiteUri,rating,googleMapsUri"
)

# Simple in-memory cache of the last search, so the CSV download route
# doesn't need to re-hit the API.
LAST_RESULTS = []
LAST_SEARCH_QUERY = ""

# In-memory store of the most recent email verification batch.
LAST_VERIFY_RESULTS = []

# --- Background job state for progress-tracked operations ---
# Each job dict is only ever touched while holding its lock.

SCRAPE_JOB = {"running": False, "total": 0, "done": 0, "started_at": None,
              "businesses_with_email": 0, "total_emails": 0}
SCRAPE_LOCK = threading.Lock()

SEARCH_JOB = {"running": False, "phase": "", "total": 0, "done": 0, "started_at": None,
              "error": None, "count": 0, "generation": 0, "last_update": None}
SEARCH_LOCK = threading.Lock()

# How many business detail lookups to run at once. Google Places details
# calls are independent of each other, so we don't need to do them one at a
# time — a small pool of concurrent workers finishes a search of 50-100
# businesses in a fraction of the time a sequential loop would take.
DETAIL_FETCH_WORKERS = 8

# If a search job's "running" flag has been stuck true with no progress for
# longer than this, treat it as dead (e.g. the worker process was killed or
# an unexpected exception escaped the background thread) instead of
# blocking every future search attempt forever.
STALE_JOB_SECONDS = 90

# Search by a pasted list of company names (instead of keyword+location).
NAME_SEARCH_JOB = {"running": False, "total": 0, "done": 0, "started_at": None,
                    "error": None, "count": 0, "not_found": [], "last_update": None}
NAME_SEARCH_LOCK = threading.Lock()

VERIFY_JOB = {"running": False, "total": 0, "done": 0, "started_at": None,
              "valid": 0, "invalid": 0, "unknown": 0, "port_25_open": None}
VERIFY_LOCK = threading.Lock()

UPLOAD_VERIFY_JOB = {"running": False, "total": 0, "done": 0, "started_at": None,
                      "valid": 0, "invalid": 0, "unknown": 0, "port_25_open": None}
UPLOAD_VERIFY_LOCK = threading.Lock()


def _eta_seconds(job):
    if not job["started_at"] or job["done"] == 0 or job["total"] <= job["done"]:
        return None
    elapsed = time.time() - job["started_at"]
    avg_per_item = elapsed / job["done"]
    return round(avg_per_item * (job["total"] - job["done"]))


def _build_download_filename(extension: str) -> str:
    """Build a descriptive filename like 'business in ikeja 2026-08-18.csv'."""
    today = time.strftime("%Y-%m-%d")
    query = (LAST_SEARCH_QUERY or "search results").strip()
    query = " ".join(query.split())
    query = query.lower()
    query = "".join(ch if ch.isalnum() or ch.isspace() or ch in "-_" else " " for ch in query)
    query = " ".join(query.split())
    query = query.strip(" -_")
    filename = f"{query} {today}" if query else f"search-results {today}"
    return f"{filename}.{extension.strip('.').lower()}"


REQUEST_TIMEOUT = 15  # seconds — without this, a stalled connection hangs forever

# Explicitly bypass any HTTP_PROXY/HTTPS_PROXY environment variables for
# calls to Google's API. Python's `requests` auto-detects and routes
# through system proxy env vars (unlike some other HTTP clients), and a
# stale/misconfigured one on the machine can cause connections to hang
# indefinitely with zero error — which looks identical to a network outage.
NO_PROXY = {"http": None, "https": None}


def _google_error_message(resp):
    """Turn a Google API error response into a clear, specific message."""
    try:
        err = resp.json().get("error", {})
        status = err.get("status", "UNKNOWN")
        message = err.get("message", resp.text)
        return f"Google API error {resp.status_code} ({status}): {message}"
    except Exception:
        return f"Google API error {resp.status_code}: {resp.text}"


def search_places(query, max_results=60, on_progress=None):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": SEARCH_FIELD_MASK,
    }
    all_places = []
    body = {"textQuery": query, "pageSize": min(20, max_results)}
    next_page_token = None

    while len(all_places) < max_results:
        if next_page_token:
            body["pageToken"] = next_page_token
            time.sleep(2)

        try:
            resp = requests.post(TEXT_SEARCH_URL, headers=headers, json=body,
                                  timeout=REQUEST_TIMEOUT, proxies=NO_PROXY)
        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"Google Places API didn't respond within {REQUEST_TIMEOUT}s. "
                "This usually means a network/firewall/antivirus/VPN issue on this machine, "
                "not a problem with the app itself — try again, or test with a plain browser "
                "request to https://places.googleapis.com to see if it's reachable at all."
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error contacting Google Places API: {e}")

        if resp.status_code != 200:
            raise RuntimeError(_google_error_message(resp))

        data = resp.json()
        places = data.get("places", [])
        all_places.extend(places)

        if on_progress:
            on_progress(len(all_places))

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    if not all_places:
        raise RuntimeError(
            f"Google returned 0 results for this exact query. This isn't an error — "
            f"it means no businesses matched. Try a more specific or more common business "
            f"type, or a more specific location (a city name usually works better than a "
            f"country or region name like 'England')."
        )

    return all_places[:max_results]


# True legal-entity suffixes only — things that get abbreviated/dropped
# inconsistently between how a company is registered and how it's actually
# listed on Google Maps. Deliberately NOT stripping words like "Solutions",
# "Consulting", "Technologies" etc. — those are usually part of the real,
# distinguishing brand name (e.g. "Dragnet Solutions"), and dropping them
# turns the fallback query into something too generic to find the right
# business at all.
_LEGAL_SUFFIXES = re.compile(
    r"\b(limited|ltd\.?|llc|plc|inc\.?|incorporated|co\.?|company)\b",
    re.IGNORECASE,
)


def _simplify_company_name(name):
    """Strip common legal/descriptor suffixes to build a looser fallback query."""
    simplified = _LEGAL_SUFFIXES.sub(" ", name)
    simplified = " ".join(simplified.split())
    return simplified.strip(" ,.-")


def _name_similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _text_search(query, page_size=5):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": SEARCH_FIELD_MASK,
    }
    body = {"textQuery": query, "pageSize": page_size}

    try:
        resp = requests.post(TEXT_SEARCH_URL, headers=headers, json=body,
                              timeout=REQUEST_TIMEOUT, proxies=NO_PROXY)
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Google Places API didn't respond within {REQUEST_TIMEOUT}s while looking up "
            f"'{query}'. This usually means a network/firewall/VPN issue on this machine."
        )
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error contacting Google Places API for '{query}': {e}")

    if resp.status_code != 200:
        raise RuntimeError(_google_error_message(resp))

    return resp.json().get("places", [])


def search_place_by_name(name, location_hint="", max_candidates=1):
    """
    Text-search for businesses by name (optionally narrowed by a location hint,
    e.g. 'Lagos, Nigeria') and return matching place(s).

    A single rigid query misses a lot of real businesses — many companies
    (especially smaller/local ones) aren't listed on Google Maps under
    exactly their registered name, or only show up once you drop the
    location hint or the "Limited"/"Ltd"-style suffix. So this tries a
    handful of query variants and collects all candidates.

    Args:
        name: Company name to search for
        location_hint: Optional location to narrow search (e.g. 'Lagos, Nigeria')
        max_candidates: Maximum number of candidates to return (default 1 for 
                       compatibility; use higher for bulk search)

    Returns:
        For max_candidates=1: single place dict or None
        For max_candidates>1: list of place dicts (sorted by similarity score)
    """
    name = name.strip()
    if not name:
        return [] if max_candidates > 1 else None

    queries = []
    if location_hint:
        queries.append(f"{name} {location_hint}")
    queries.append(name)
    simplified = _simplify_company_name(name)
    if simplified and simplified.lower() != name.lower():
        if location_hint:
            queries.append(f"{simplified} {location_hint}")
        queries.append(simplified)

    # Collect all candidates with their scores
    candidates = {}  # place_id -> (place, score)

    for query in queries:
        places = _text_search(query, page_size=5)
        for place in places:
            place_id = place.get("id")
            if not place_id:
                continue
            
            # Skip if we already have this place from a better query
            if place_id in candidates:
                continue
                
            candidate_name = place.get("displayName", {}).get("text", "")
            score = _name_similarity(name, candidate_name)
            candidates[place_id] = (place, score)

        # Good enough match found — no need to try looser fallback queries.
        if candidates and max(score for _, score in candidates.values()) >= 0.6:
            break

    # Sort by score (descending) and return
    sorted_candidates = sorted(candidates.values(), key=lambda x: x[1], reverse=True)
    results = [place for place, _ in sorted_candidates[:max_candidates]]

    # Return single result or list based on max_candidates
    if max_candidates == 1:
        return results[0] if results else None
    return results


def get_place_details(place_id):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": DETAILS_FIELD_MASK,
    }
    try:
        resp = requests.get(DETAILS_URL.format(place_id=place_id), headers=headers,
                             timeout=REQUEST_TIMEOUT, proxies=NO_PROXY)
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.json()


@app.route("/")
def index():
    key_configured = bool(API_KEY) and API_KEY != "your_key_here"
    return render_template("index.html", key_configured=key_configured)


@app.route("/verify")
def verify_page():
    return render_template("verify.html")


def _place_to_row(details):
    return {
        "name": details.get("displayName", {}).get("text", ""),
        "phone": details.get("internationalPhoneNumber") or details.get("nationalPhoneNumber", ""),
        "address": details.get("formattedAddress", ""),
        "website": details.get("websiteUri", ""),
        "rating": details.get("rating", ""),
        "maps_url": details.get("googleMapsUri", ""),
        "email": "",
        "all_emails": "",
        "website_phone": "",
        "call_status": "",
        "scraped": False,
        "email_verification": "",
        "email_verified": False,
    }


def _run_search_job(keyword, location, limit, generation):
    global LAST_RESULTS

    query = f"{keyword} in {location}"

    with SEARCH_LOCK:
        if SEARCH_JOB["generation"] != generation:
            return  # superseded before we even got going
        SEARCH_JOB.update(running=True, phase="searching", total=0, done=0,
                           started_at=time.time(), last_update=time.time(),
                           error=None, count=0)

    def on_search_progress(found_so_far):
        with SEARCH_LOCK:
            if SEARCH_JOB["generation"] != generation:
                return
            SEARCH_JOB["total"] = max(SEARCH_JOB["total"], min(found_so_far, limit))
            SEARCH_JOB["last_update"] = time.time()

    try:
        places = search_places(query, max_results=limit, on_progress=on_search_progress)
    except RuntimeError as e:
        with SEARCH_LOCK:
            if SEARCH_JOB["generation"] == generation:
                SEARCH_JOB.update(running=False, error=str(e), last_update=time.time())
        return

    with SEARCH_LOCK:
        if SEARCH_JOB["generation"] != generation:
            return
        SEARCH_JOB.update(phase="fetching_details", total=len(places), done=0,
                           started_at=time.time(), last_update=time.time())

    # Fetch details for every place concurrently instead of one request at a
    # time — this is the main reason a search of 50-100 businesses used to
    # take several minutes.
    rows = [None] * len(places)

    def fetch_one(index, place_id):
        return index, (get_place_details(place_id) if place_id else None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=DETAIL_FETCH_WORKERS) as executor:
        futures = [executor.submit(fetch_one, i, p.get("id")) for i, p in enumerate(places)]
        for future in concurrent.futures.as_completed(futures):
            index, details = future.result()
            if details:
                rows[index] = _place_to_row(details)

            with SEARCH_LOCK:
                if SEARCH_JOB["generation"] != generation:
                    # A newer search has taken over — stop touching shared
                    # state, but let already-in-flight requests finish
                    # quietly so we don't leave dangling connections.
                    return
                SEARCH_JOB["done"] += 1
                SEARCH_JOB["last_update"] = time.time()

    with SEARCH_LOCK:
        if SEARCH_JOB["generation"] != generation:
            return

    LAST_RESULTS = [r for r in rows if r is not None]

    with SEARCH_LOCK:
        if SEARCH_JOB["generation"] == generation:
            SEARCH_JOB.update(running=False, count=len(LAST_RESULTS), last_update=time.time())


@app.route("/search/start", methods=["POST"])
def search_start():
    if not API_KEY or API_KEY == "your_key_here":
        return jsonify({
            "error": "No API key configured. Open the .env file in this folder "
                     "and replace 'your_key_here' with your real Google Places API key."
        }), 400

    with SEARCH_LOCK:
        if SEARCH_JOB["running"]:
            last_update = SEARCH_JOB["last_update"]
            stale = last_update is not None and (time.time() - last_update) > STALE_JOB_SECONDS
            if not stale:
                return jsonify({"error": "A search is already running."}), 409
            # No progress for a while — most likely the previous job's
            # worker/thread died or the server restarted mid-search. Let a
            # new search take over instead of blocking forever.

    payload = request.get_json()
    keyword = (payload.get("keyword") or "").strip()
    location = (payload.get("location") or "").strip()
    limit = int(payload.get("limit") or 20)
    limit = max(1, min(limit, 100))  # cap at 100 per search to control cost

    if not keyword or not location:
        return jsonify({"error": "Please enter both a business type and a location."}), 400

    global LAST_SEARCH_QUERY
    LAST_SEARCH_QUERY = f"{keyword} in {location}".strip()

    with SEARCH_LOCK:
        SEARCH_JOB["generation"] += 1
        generation = SEARCH_JOB["generation"]

    threading.Thread(target=_run_search_job, args=(keyword, location, limit, generation), daemon=True).start()
    return jsonify({"started": True})


@app.route("/search/progress")
def search_progress():
    with SEARCH_LOCK:
        job = dict(SEARCH_JOB)

    return jsonify({
        "running": job["running"],
        "phase": job["phase"],
        "done": job["done"],
        "total": job["total"],
        "error": job["error"],
        "count": job["count"],
        "eta_seconds": _eta_seconds(job),
        "results": LAST_RESULTS,
    })


def _run_name_search_job(names, location_hint, max_candidates_per_name=3):
    """
    Search for multiple businesses by name. For each company name, try to find
    up to max_candidates_per_name matches and add them all to results.
    This gives more comprehensive results in bulk searches.
    """
    global LAST_RESULTS

    with NAME_SEARCH_LOCK:
        NAME_SEARCH_JOB.update(running=True, total=len(names), done=0,
                                started_at=time.time(), last_update=time.time(),
                                error=None, count=0, not_found=[])

    LAST_RESULTS = []
    not_found = []

    for name in names:
        places = []
        try:
            places = search_place_by_name(name, location_hint, max_candidates=max_candidates_per_name)
            # Handle case where function returns single place or list
            if isinstance(places, dict):
                places = [places]
            elif not places:
                places = []
        except RuntimeError as e:
            # Keep going — one bad lookup shouldn't kill the whole batch.
            # We surface the last error message so the user knows something's wrong.
            with NAME_SEARCH_LOCK:
                NAME_SEARCH_JOB["error"] = str(e)

        if places:
            found_for_this_name = 0
            for place in places:
                place_id = place.get("id")
                details = get_place_details(place_id) if place_id else None
                if details:
                    LAST_RESULTS.append({
                        "name": details.get("displayName", {}).get("text", "") or name,
                        "phone": details.get("internationalPhoneNumber") or details.get("nationalPhoneNumber", ""),
                        "address": details.get("formattedAddress", ""),
                        "website": details.get("websiteUri", ""),
                        "rating": details.get("rating", ""),
                        "maps_url": details.get("googleMapsUri", ""),
                        "email": "",
                        "all_emails": "",
                        "website_phone": "",
                        "call_status": "",
                        "scraped": False,
                        "email_verification": "",
                        "email_verified": False,
                        "matched_query": name,
                    })
                    found_for_this_name += 1
                    time.sleep(0.1)  # Small delay between detail fetches
            
            if found_for_this_name == 0:
                not_found.append(name)
        else:
            not_found.append(name)

        with NAME_SEARCH_LOCK:
            NAME_SEARCH_JOB["done"] += 1
            NAME_SEARCH_JOB["not_found"] = list(not_found)
            NAME_SEARCH_JOB["last_update"] = time.time()

        time.sleep(0.1)

    with NAME_SEARCH_LOCK:
        NAME_SEARCH_JOB.update(running=False, count=len(LAST_RESULTS), last_update=time.time())


@app.route("/search-by-name/start", methods=["POST"])
def search_by_name_start():
    if not API_KEY or API_KEY == "your_key_here":
        return jsonify({
            "error": "No API key configured. Open the .env file in this folder "
                     "and replace 'your_key_here' with your real Google Places API key."
        }), 400

    with NAME_SEARCH_LOCK:
        if NAME_SEARCH_JOB["running"]:
            last_update = NAME_SEARCH_JOB["last_update"]
            stale = last_update is not None and (time.time() - last_update) > STALE_JOB_SECONDS
            if not stale:
                return jsonify({"error": "A search is already running."}), 409

    payload = request.get_json() or {}
    raw_names = payload.get("names") or ""
    location_hint = (payload.get("location") or "").strip()

    # Accept names separated by newlines, commas, semicolons, or tabs (people
    # paste from all sorts of sources: a plain list, a numbered list, a
    # spreadsheet column pasted as text, etc.) — one company per entry.
    raw_lines = re.split(r"[\n,;\t]+", raw_names)
    seen = set()
    names = []
    for line in raw_lines:
        cleaned = line.strip()
        # Strip common list markers: "1.", "1)", "-", "*", "•"
        cleaned = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", cleaned).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            names.append(cleaned)

    if not names:
        return jsonify({"error": "Please paste at least one company name (one per line)."}), 400
    if len(names) > 200:
        return jsonify({"error": "Please limit a batch to 200 company names at a time."}), 400

    global LAST_SEARCH_QUERY
    LAST_SEARCH_QUERY = f"{len(names)} companies" + (f" in {location_hint}" if location_hint else "")

    threading.Thread(target=_run_name_search_job, args=(names, location_hint), daemon=True).start()
    return jsonify({"started": True, "total": len(names)})


@app.route("/search-by-name/progress")
def search_by_name_progress():
    with NAME_SEARCH_LOCK:
        job = dict(NAME_SEARCH_JOB)

    return jsonify({
        "running": job["running"],
        "done": job["done"],
        "total": job["total"],
        "error": job["error"],
        "count": job["count"],
        "not_found": job["not_found"],
        "eta_seconds": _eta_seconds(job),
        "results": LAST_RESULTS,
    })


def _run_scrape_job():
    global LAST_RESULTS

    targets = [row for row in LAST_RESULTS
               if (row.get("website") or "").strip() and not row.get("scraped")]

    with SCRAPE_LOCK:
        SCRAPE_JOB.update(running=True, total=len(targets), done=0, started_at=time.time(),
                           businesses_with_email=0, total_emails=0)

    for row in targets:
        info = find_contact_info(row["website"].strip())
        row["email"] = info["emails"][0] if info["emails"] else ""
        row["all_emails"] = "; ".join(info["emails"])
        row["website_phone"] = info["phones"][0] if info["phones"] else ""
        row["scraped"] = True

        with SCRAPE_LOCK:
            SCRAPE_JOB["done"] += 1
            if info["emails"]:
                SCRAPE_JOB["businesses_with_email"] += 1
                SCRAPE_JOB["total_emails"] += len(info["emails"])

        time.sleep(0.5)  # be polite to the businesses' own servers

    with SCRAPE_LOCK:
        SCRAPE_JOB["running"] = False


@app.route("/scrape-emails/start", methods=["POST"])
def scrape_emails_start():
    if not LAST_RESULTS:
        return jsonify({"error": "No results yet — run a search first."}), 400

    with SCRAPE_LOCK:
        if SCRAPE_JOB["running"]:
            return jsonify({"error": "A scrape is already running."}), 409

    threading.Thread(target=_run_scrape_job, daemon=True).start()
    return jsonify({"started": True})


@app.route("/scrape-emails/progress")
def scrape_emails_progress():
    with SCRAPE_LOCK:
        job = dict(SCRAPE_JOB)

    return jsonify({
        "running": job["running"],
        "done": job["done"],
        "total": job["total"],
        "businesses_with_email": job["businesses_with_email"],
        "total_emails": job["total_emails"],
        "eta_seconds": _eta_seconds(job),
        "results": LAST_RESULTS,
    })


def _run_verify_job():
    global LAST_RESULTS

    port_25_open = check_port_25()

    pending = []  # (row, [emails]) for rows not yet verified
    for row in LAST_RESULTS:
        if row.get("email_verified"):
            continue
        raw = row.get("all_emails") or row.get("email") or ""
        emails = [e.strip() for e in raw.split(";") if e.strip()]
        if emails:
            pending.append((row, emails))

    total = sum(len(emails) for _, emails in pending)

    with VERIFY_LOCK:
        VERIFY_JOB.update(running=True, total=total, done=0, started_at=time.time(),
                           valid=0, invalid=0, unknown=0, port_25_open=port_25_open)

    for row, emails in pending:
        statuses = []
        for email in emails:
            outcome = verify_email(email)
            statuses.append(f"{outcome['email']} ({outcome['status']})")

            with VERIFY_LOCK:
                VERIFY_JOB["done"] += 1
                if outcome["status"] == "valid":
                    VERIFY_JOB["valid"] += 1
                elif outcome["status"] == "invalid":
                    VERIFY_JOB["invalid"] += 1
                else:
                    VERIFY_JOB["unknown"] += 1

            time.sleep(0.3)

        row["email_verification"] = "; ".join(statuses)
        row["email_verified"] = True

    with VERIFY_LOCK:
        VERIFY_JOB["running"] = False


@app.route("/verify-current/start", methods=["POST"])
def verify_current_start():
    if not LAST_RESULTS:
        return jsonify({"error": "No results yet — run a search first."}), 400

    with VERIFY_LOCK:
        if VERIFY_JOB["running"]:
            return jsonify({"error": "A verification job is already running."}), 409

    threading.Thread(target=_run_verify_job, daemon=True).start()
    return jsonify({"started": True})


@app.route("/verify-current/progress")
def verify_current_progress():
    with VERIFY_LOCK:
        job = dict(VERIFY_JOB)

    return jsonify({
        "running": job["running"],
        "done": job["done"],
        "total": job["total"],
        "valid": job["valid"],
        "invalid": job["invalid"],
        "unknown": job["unknown"],
        "port_25_open": job["port_25_open"],
        "eta_seconds": _eta_seconds(job),
        "results": LAST_RESULTS,
    })


CSV_FIELDS = ["name", "phone", "website_phone", "address", "website", "email", "all_emails", "email_verification", "rating", "maps_url", "call_status"]


@app.route("/download-csv")
def download_csv():
    if not LAST_RESULTS:
        return "No results yet — run a search first.", 400

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(LAST_RESULTS)

    filename = _build_download_filename("csv")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/download-pdf")
def download_pdf():
    if not LAST_RESULTS:
        return "No results yet — run a search first.", 400

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, leading=10)
    header_style = ParagraphStyle("header", parent=styles["Normal"], fontSize=8, leading=10,
                                   textColor=colors.white, fontName="Helvetica-Bold")

    story = []
    story.append(Paragraph("Leads Report", styles["Title"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"{len(LAST_RESULTS)} businesses", styles["Normal"]))
    story.append(Spacer(1, 12))

    headers = ["Name", "Address", "Phone (Places)", "Phone (Website)", "Email(s)", "Website"]
    table_data = [[Paragraph(h, header_style) for h in headers]]

    for row in LAST_RESULTS:
        places_phone = row.get("phone") or "—"
        website_phone = row.get("website_phone") or ""
        # Flag when the website's own number differs from the one Google has on file
        if website_phone and website_phone.strip() != places_phone.strip():
            website_phone_display = website_phone
        elif website_phone:
            website_phone_display = website_phone + " (same)"
        else:
            website_phone_display = "—"

        emails = row.get("all_emails") or row.get("email") or "—"

        table_data.append([
            Paragraph(row.get("name", ""), cell_style),
            Paragraph(row.get("address", ""), cell_style),
            Paragraph(places_phone, cell_style),
            Paragraph(website_phone_display, cell_style),
            Paragraph(emails, cell_style),
            Paragraph(row.get("website", "") or "—", cell_style),
        ])

    col_widths = [45 * mm, 65 * mm, 30 * mm, 35 * mm, 55 * mm, 45 * mm]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2a2e38")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)

    doc.build(story)
    buf.seek(0)

    filename = _build_download_filename("pdf")
    return Response(
        buf.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/download-docx")
def download_docx():
    if not LAST_RESULTS:
        return "No results yet — run a search first.", 400

    try:
        from docx import Document
    except ImportError:
        return "DOCX export requires the python-docx package. Install it with: pip install python-docx", 500

    doc = Document()
    doc.add_heading("Lead Call Sheet", level=1)
    doc.add_paragraph(f"Search: {LAST_SEARCH_QUERY or 'Search results'}")
    doc.add_paragraph("Use this column to record: Called, Call Again, Didn’t Pick, Not Interested, Wrong Number, Follow Up.")

    headers = ["Name", "Phone", "Address", "Website", "Email(s)", "Website Phone", "Rating", "Maps URL", "Call Status"]
    table = doc.add_table(rows=1, cols=len(headers))
    for i, header in enumerate(headers):
        table.rows[0].cells[i].text = header

    for row in LAST_RESULTS:
        cells = table.add_row().cells
        cells[0].text = row.get("name", "")
        cells[1].text = row.get("phone", "")
        cells[2].text = row.get("address", "")
        cells[3].text = row.get("website", "")
        cells[4].text = row.get("all_emails") or row.get("email") or ""
        cells[5].text = row.get("website_phone", "")
        cells[6].text = str(row.get("rating", ""))
        cells[7].text = row.get("maps_url", "")
        cells[8].text = row.get("call_status", "")

    table.style = "Table Grid"
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    filename = _build_download_filename("docx")
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# Backward-compatible alias for older references/tests.
def download_doc():
    return download_docx()


def _run_upload_verify_job(pairs):
    global LAST_VERIFY_RESULTS
    LAST_VERIFY_RESULTS = []

    port_25_open = check_port_25()

    with UPLOAD_VERIFY_LOCK:
        UPLOAD_VERIFY_JOB.update(running=True, total=len(pairs), done=0, started_at=time.time(),
                                  valid=0, invalid=0, unknown=0, port_25_open=port_25_open)

    for entry in pairs:
        outcome = verify_email(entry["email"])
        LAST_VERIFY_RESULTS.append({
            "name": entry["name"],
            "email": outcome["email"],
            "status": outcome["status"],
            "reason": outcome["reason"],
        })

        with UPLOAD_VERIFY_LOCK:
            UPLOAD_VERIFY_JOB["done"] += 1
            if outcome["status"] == "valid":
                UPLOAD_VERIFY_JOB["valid"] += 1
            elif outcome["status"] == "invalid":
                UPLOAD_VERIFY_JOB["invalid"] += 1
            else:
                UPLOAD_VERIFY_JOB["unknown"] += 1

        time.sleep(0.3)  # be gentle with mail servers we're probing

    with UPLOAD_VERIFY_LOCK:
        UPLOAD_VERIFY_JOB["running"] = False


@app.route("/verify-upload/start", methods=["POST"])
def verify_upload_start():
    with UPLOAD_VERIFY_LOCK:
        if UPLOAD_VERIFY_JOB["running"]:
            return jsonify({"error": "A verification job is already running."}), 409

    if "file" not in request.files or request.files["file"].filename == "":
        return jsonify({"error": "Please choose a CSV or PDF file to upload."}), 400

    uploaded = request.files["file"]
    filename = uploaded.filename
    file_bytes = uploaded.read()

    try:
        pairs = parse_upload(filename, file_bytes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Couldn't read that file: {e}"}), 400

    if not pairs:
        return jsonify({"error": "No email addresses were found in that file."}), 400

    # De-dupe by email while keeping the first business name seen for it
    seen = {}
    for p in pairs:
        key = p["email"].lower()
        if key not in seen:
            seen[key] = p

    dedup_pairs = list(seen.values())
    threading.Thread(target=_run_upload_verify_job, args=(dedup_pairs,), daemon=True).start()
    return jsonify({"started": True, "total": len(dedup_pairs)})


@app.route("/verify-upload/progress")
def verify_upload_progress():
    with UPLOAD_VERIFY_LOCK:
        job = dict(UPLOAD_VERIFY_JOB)

    return jsonify({
        "running": job["running"],
        "done": job["done"],
        "total": job["total"],
        "valid": job["valid"],
        "invalid": job["invalid"],
        "unknown": job["unknown"],
        "port_25_open": job["port_25_open"],
        "eta_seconds": _eta_seconds(job),
        "results": LAST_VERIFY_RESULTS,
    })


@app.route("/verify-download-csv")
def verify_download_csv():
    if not LAST_VERIFY_RESULTS:
        return "No verification results yet — upload a file first.", 400

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["name", "email", "status", "reason"])
    writer.writeheader()
    writer.writerows(LAST_VERIFY_RESULTS)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=verified_emails.csv"},
    )


def _verify_resend_webhook(payload: bytes, svix_id: str, svix_timestamp: str, svix_signature: str) -> bool:
    """
    Verify a Resend/Svix webhook signature so we don't act on spoofed
    bounce events. Scheme: HMAC-SHA256(secret, "{id}.{timestamp}.{body}"),
    base64-encoded, compared against any value in the space-separated
    svix-signature header (each formatted "v1,<base64>").
    """
    if not RESEND_WEBHOOK_SECRET or not (svix_id and svix_timestamp and svix_signature):
        return False

    # Reject stale deliveries (replay protection), per Resend/Svix guidance
    try:
        if abs(time.time() - int(svix_timestamp)) > 300:
            return False
    except ValueError:
        return False

    secret_bytes = base64.b64decode(RESEND_WEBHOOK_SECRET.removeprefix("whsec_"))
    signed_content = f"{svix_id}.{svix_timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    expected = base64.b64encode(hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()).decode()

    for part in svix_signature.split():
        _, _, sig = part.partition(",")
        if hmac.compare_digest(sig, expected):
            return True
    return False


@app.route("/webhook/resend", methods=["POST"])
def resend_webhook():
    """
    Receives bounce/complaint events from Resend and auto-suppresses
    those addresses so they're never emailed again. Requires a
    PUBLICLY REACHABLE URL — Resend's servers can't reach 127.0.0.1,
    so this only works if the app is deployed somewhere reachable, or
    you're tunnelling to it (e.g. `ngrok http 5000`) during testing.
    Point Resend's webhook dashboard at: https://your-public-url/webhook/resend
    """
    raw_body = request.get_data()
    svix_id = request.headers.get("svix-id", "")
    svix_timestamp = request.headers.get("svix-timestamp", "")
    svix_signature = request.headers.get("svix-signature", "")

    if not _verify_resend_webhook(raw_body, svix_id, svix_timestamp, svix_signature):
        return jsonify({"error": "invalid signature"}), 400

    event = request.get_json(silent=True) or {}
    event_type = event.get("type", "")
    data = event.get("data", {})
    recipients = data.get("to", [])

    should_suppress = False
    reason = ""

    if event_type == "email.bounced" and data.get("bounce_type") == "hard":
        should_suppress = True
        reason = f"hard bounce: {data.get('bounce_reason', 'unknown')}"
    elif event_type == "email.complained":
        should_suppress = True
        reason = "spam complaint"

    if should_suppress:
        is_new = not os.path.exists(SUPPRESSION_PATH)
        with open(SUPPRESSION_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["email", "reason", "added_at"])
            for addr in recipients:
                writer.writerow([addr, reason, time.strftime("%Y-%m-%d %H:%M:%S")])

    # Always ack quickly with 2xx so Resend doesn't retry
    return jsonify({"received": True}), 200


if __name__ == "__main__":
    # use_reloader=False: prevents the dev server from restarting mid-request
    # if a .py file in this folder gets touched (editor autosave, git, etc.),
    # which otherwise silently kills in-flight requests. You'll need to
    # manually re-run `python app.py` after making real code changes.
    app.run(debug=True, port=5000, threaded=True, use_reloader=False)
