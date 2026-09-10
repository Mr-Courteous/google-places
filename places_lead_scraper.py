"""
Google Places API (New) lead fetcher
-------------------------------------
Legitimate, ToS-compliant way to pull business contact data (name, phone,
address, website) for a given search term + location. Does NOT scrape
Google Maps directly (that violates Google's ToS and can get you blocked).

SETUP:
1. Go to https://console.cloud.google.com/
2. Create a project (or use an existing one)
3. Enable "Places API (New)"
4. Create an API key under APIs & Services > Credentials
5. (Recommended) Restrict the key to Places API only + your IP
6. Set the API_KEY variable below, or export it as an env var:
       export GOOGLE_PLACES_API_KEY="your_key_here"

COST NOTE:
Google gives a monthly free credit, but Places API (New) charges per
request past that. Check current pricing at:
https://developers.google.com/maps/documentation/places/web-service/usage-and-billing
Test with a small radius/query first before running a big batch.

USAGE:
    python places_lead_scraper.py "restaurants" "Kano, Nigeria" --limit 60

OUTPUT:
    leads.csv with columns: name, phone, address, website, rating, maps_url
"""

import os
import sys
import csv
import time
import argparse
import requests

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "PASTE_YOUR_KEY_HERE")

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Fields returned by the initial text search (cheap, "Basic" tier)
SEARCH_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,nextPageToken"

# Fields returned by the per-place details call (needed for phone/website)
DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,internationalPhoneNumber,"
    "nationalPhoneNumber,websiteUri,rating,googleMapsUri"
)


GEOCODE_FIELD_MASK = "places.location,places.viewport,places.formattedAddress"


def _geocode_area(location):
    """Resolve a typed location to a real geographic viewport via the
    Places API, so the search can be geographically restricted instead of
    relying on fuzzy text matching of 'keyword in <location>'."""
    if not location:
        return None
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": GEOCODE_FIELD_MASK,
    }
    body = {"textQuery": location, "pageSize": 1}
    try:
        resp = requests.post(TEXT_SEARCH_URL, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        places = resp.json().get("places", [])
    except requests.exceptions.RequestException:
        return None
    if not places:
        return None
    place = places[0]
    viewport = place.get("viewport")
    if viewport and "low" in viewport and "high" in viewport:
        return viewport
    loc = place.get("location")
    if not loc or loc.get("latitude") is None or loc.get("longitude") is None:
        return None
    lat, lng = loc["latitude"], loc["longitude"]
    delta = 0.022
    return {
        "low": {"latitude": lat - delta, "longitude": lng - delta},
        "high": {"latitude": lat + delta, "longitude": lng + delta},
    }


def _location_matches(address, location):
    """Loosely check whether a place's formatted address actually falls
    within the location typed (e.g. 'Ikeja'), rather than a nearby area
    Google's fuzzy text search pulled in anyway (e.g. 'Oshodi'). Used only
    as a fallback when geocoding the typed location fails."""
    if not location:
        return True
    primary = location.split(",")[0].strip().lower()
    if not primary:
        return True
    return primary in (address or "").lower()


def search_places(query: str, max_results: int = 60, location: str = None):
    """Text-search for a query (e.g. 'gyms in Kano') and return place IDs.

    Note: Google's Places Text Search API caps out at 60 results (3 pages
    of 20) per distinct query, no matter how high max_results is set — this
    is a Google-side limit, not something this script controls.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": SEARCH_FIELD_MASK,
    }
    all_places = []
    location_restriction = _geocode_area(location) if location else None
    body = {"textQuery": query, "pageSize": min(20, max_results)}
    if location_restriction:
        body["locationRestriction"] = {"rectangle": location_restriction}
    next_page_token = None

    while len(all_places) < max_results:
        if next_page_token:
            body["pageToken"] = next_page_token
            # Google requires a short delay before a page token becomes valid
            time.sleep(2)

        resp = requests.post(TEXT_SEARCH_URL, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"Search error {resp.status_code}: {resp.text}")
            break

        data = resp.json()
        raw_places = data.get("places", [])
        if location and not location_restriction:
            places = [p for p in raw_places if _location_matches(p.get("formattedAddress", ""), location)]
        else:
            places = raw_places
        all_places.extend(places)

        next_page_token = data.get("nextPageToken")
        if not next_page_token or not raw_places:
            break

    return all_places[:max_results]


def get_place_details(place_id: str):
    """Fetch phone number, website, rating for a single place ID."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": DETAILS_FIELD_MASK,
    }
    resp = requests.get(DETAILS_URL.format(place_id=place_id), headers=headers)
    if resp.status_code != 200:
        print(f"Details error {resp.status_code}: {resp.text}")
        return None
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Fetch business leads via Google Places API")
    parser.add_argument("keyword", help="e.g. 'restaurants', 'gyms', 'car dealers'")
    parser.add_argument("location", help="e.g. 'Kano, Nigeria'")
    parser.add_argument("--limit", type=int, default=60, help="Max number of results (default 60)")
    parser.add_argument("--out", default="leads.csv", help="Output CSV filename")
    args = parser.parse_args()

    if API_KEY == "PASTE_YOUR_KEY_HERE":
        print("ERROR: Set GOOGLE_PLACES_API_KEY env var or edit API_KEY in the script.")
        sys.exit(1)

    query = f"{args.keyword} in {args.location}"
    print(f"Searching: {query}")
    places = search_places(query, max_results=args.limit, location=args.location)
    print(f"Found {len(places)} places. Fetching details...")

    rows = []
    for i, p in enumerate(places, 1):
        place_id = p.get("id")
        if not place_id:
            continue
        details = get_place_details(place_id)
        if not details:
            continue

        name = details.get("displayName", {}).get("text", "")
        phone = details.get("internationalPhoneNumber") or details.get("nationalPhoneNumber", "")
        address = details.get("formattedAddress", "")
        website = details.get("websiteUri", "")
        rating = details.get("rating", "")
        maps_url = details.get("googleMapsUri", "")

        rows.append({
            "name": name,
            "phone": phone,
            "address": address,
            "website": website,
            "rating": rating,
            "maps_url": maps_url,
        })
        print(f"  [{i}/{len(places)}] {name} — {phone or 'no phone listed'}")

        time.sleep(0.1)  # be gentle on rate limits

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "phone", "address", "website", "rating", "maps_url"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. Saved {len(rows)} leads to {args.out}")
    with_phone = sum(1 for r in rows if r["phone"])
    with_site = sum(1 for r in rows if r["website"])
    print(f"  {with_phone}/{len(rows)} have a phone number")
    print(f"  {with_site}/{len(rows)} have a website (needed for step 2: email lookup)")


if __name__ == "__main__":
    main()
