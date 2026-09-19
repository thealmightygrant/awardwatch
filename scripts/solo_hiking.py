#!/usr/bin/env python3
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date
from pathlib import Path

BASE = "https://seats.aero/partnerapi"
NYC = {"JFK", "EWR", "LGA"}
DESTINATION_REGIONS = ["North America", "South America", "Europe", "Africa", "Asia", "Oceania"]
END_DATE = "2026-12-31"
MAX_PROGRAM_POINTS = 100_000
TAKE = 1000
MAX_DETAIL_LOOKUPS = 250
TRIPS_PER_AVAILABILITY = 3

# Current AmEx US transfer partners that Seats.aero supports directly, plus United.
# mr_per_program_point is used only as a comparison aid; actual transfers are in
# program-specific increments and should be verified with AmEx before transferring.
PROGRAMS = {
    "aeroplan": {
        "name": "Air Canada Aeroplan",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "flyingblue": {
        "name": "Air France/KLM Flying Blue",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "virginatlantic": {
        "name": "Virgin Atlantic Flying Club",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "delta": {
        "name": "Delta SkyMiles",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": True,
    },
    "emirates": {
        "name": "Emirates Skywards",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.25,  # 1,000 MR -> 800 Skywards
        "has_seat_count": False,
    },
    "qantas": {
        "name": "Qantas Frequent Flyer",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": False,
    },
    "qatar": {
        "name": "Qatar Airways Privilege Club",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": False,
    },
    "singapore": {
        "name": "Singapore KrisFlyer",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.0,
        "has_seat_count": False,
    },
    "aeromexico": {
        "name": "Aeromexico Rewards",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 0.625,  # 1,000 MR -> 1,600 Rewards points
        "has_seat_count": True,
    },
    "jetblue": {
        "name": "JetBlue TrueBlue",
        "funding": "AmEx Membership Rewards",
        "mr_per_program_point": 1.25,  # 250 MR -> 200 TrueBlue
        "has_seat_count": True,
    },
    "united": {
        "name": "United MileagePlus",
        "funding": "United miles",
        "mr_per_program_point": None,
        "has_seat_count": True,
    },
}

API_KEY = os.environ.get("SEATS_AERO_API_KEY")
if not API_KEY:
    raise SystemExit("SEATS_AERO_API_KEY is not set")

TODAY = datetime.now(timezone.utc).date().isoformat()
if TODAY > END_DATE:
    raise SystemExit("Solo hiking watch has expired for 2026.")

def api_get(path, params=None, retries=3):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Partner-Authorization": API_KEY,
            "Accept": "application/json",
            "User-Agent": "awardwatch/1.0",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Seats.aero HTTP {e.code}: {body[:1200]}") from e
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(2 ** attempt)

def items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "trips", "availabilityTrips", "AvailabilityTrips", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []

def paginate(path, params, max_pages=100):
    out = []
    cursor = None
    skip = 0
    seen = set()
    for _ in range(max_pages):
        page_params = dict(params)
        page_params["take"] = TAKE
        if cursor is not None:
            page_params["cursor"] = cursor
            page_params["skip"] = skip
        payload = api_get(path, page_params)
        batch = items(payload)
        for row in batch:
            row_id = row.get("ID") or row.get("id")
            key = row_id or json.dumps(row, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        if cursor is None and isinstance(payload, dict):
            cursor = payload.get("cursor")
        skip += len(batch)
        if not isinstance(payload, dict) or not payload.get("hasMore") or not batch:
            break
    return out

def nint(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

def route_fields(row):
    route = row.get("Route") or row.get("route") or {}
    origin = route.get("OriginAirport") or route.get("originAirport") or row.get("OriginAirport")
    destination = route.get("DestinationAirport") or route.get("destinationAirport") or row.get("DestinationAirport")
    return origin, destination

def mr_equivalent(points, source):
    if points is None:
        return None
    ratio = PROGRAMS[source]["mr_per_program_point"]
    if ratio is None:
        return None
    # AmEx transfers generally happen in fixed increments; round up to the
    # next 1,000 MR as a practical estimate.
    raw = points * ratio
    return int(math.ceil(raw / 1000.0) * 1000)

def summary(row, source, region):
    origin, destination = route_fields(row)
    points = nint(row.get("JMileageCost"))
    seats = nint(row.get("JRemainingSeats"))
    return {
        "source": source,
        "program": PROGRAMS[source]["name"],
        "funding": PROGRAMS[source]["funding"],
        "origin": origin,
        "destination": destination,
        "destination_region": region,
        "date": row.get("Date") or row.get("date"),
        "availability_id": row.get("ID") or row.get("id"),
        "business_available": bool(row.get("JAvailable")),
        "program_points": points,
        "approx_amex_mr": mr_equivalent(points, source),
        "remaining_seats": seats,
        "seat_count_reliable": PROGRAMS[source]["has_seat_count"],
        "airlines": row.get("JAirlines"),
        "direct": bool(row.get("JDirect")),
        "updated_at": row.get("UpdatedAt") or row.get("updatedAt"),
    }

def qualifies(row, source, region):
    s = summary(row, source, region)
    if s["origin"] not in NYC or not s["destination"] or s["destination"] in NYC:
        return False
    if not s["business_available"]:
        return False
    if s["program_points"] is None or s["program_points"] >= MAX_PROGRAM_POINTS:
        return False
    if PROGRAMS[source]["has_seat_count"] and (s["remaining_seats"] or 0) < 1:
        return False
    return True

def trip_summary(trip, source):
    points = nint(trip.get("MileageCost") or trip.get("mileageCost"))
    seats = nint(trip.get("RemainingSeats") or trip.get("remainingSeats"))
    segs = trip.get("AvailabilitySegments") or trip.get("availabilitySegments") or trip.get("segments") or []
    clean_segments = []
    for seg in segs:
        clean_segments.append({
            "flight_number": seg.get("FlightNumber") or seg.get("flightNumber"),
            "origin": seg.get("OriginAirport") or seg.get("originAirport"),
            "destination": seg.get("DestinationAirport") or seg.get("destinationAirport"),
            "departs_at": seg.get("DepartsAt") or seg.get("departsAt"),
            "arrives_at": seg.get("ArrivesAt") or seg.get("arrivesAt"),
            "aircraft": seg.get("AircraftCode") or seg.get("AircraftName") or seg.get("aircraftCode"),
            "fare_class": seg.get("FareClass") or seg.get("fareClass"),
        })
    return {
        "trip_id": trip.get("ID") or trip.get("id"),
        "cabin": trip.get("Cabin") or trip.get("cabin"),
        "program_points": points,
        "approx_amex_mr": mr_equivalent(points, source),
        "remaining_seats": seats,
        "seat_count_reliable": PROGRAMS[source]["has_seat_count"],
        "taxes_minor_units": nint(trip.get("TotalTaxes") or trip.get("totalTaxes")),
        "tax_currency": trip.get("TaxesCurrency") or trip.get("taxesCurrency"),
        "tax_currency_symbol": trip.get("TaxesCurrencySymbol") or trip.get("taxesCurrencySymbol"),
        "stops": nint(trip.get("Stops") or trip.get("stops")),
        "carriers": trip.get("Carriers") or trip.get("carriers"),
        "flight_numbers": trip.get("FlightNumbers") or trip.get("flightNumbers"),
        "departs_at": trip.get("DepartsAt") or trip.get("departsAt"),
        "arrives_at": trip.get("ArrivesAt") or trip.get("arrivesAt"),
        "mixed_cabin_pct": trip.get("MixedCabinPct") or trip.get("mixedCabinPct"),
        "segments": clean_segments,
    }

def get_business_trips(availability_id, source):
    payload = api_get(
        f"/trips/{urllib.parse.quote(str(availability_id))}",
        {"min_cabin_pct": 100},
    )
    result = []
    for trip in items(payload):
        cabin = str(trip.get("Cabin") or trip.get("cabin") or "").lower()
        points = nint(trip.get("MileageCost") or trip.get("mileageCost"))
        seats = nint(trip.get("RemainingSeats") or trip.get("remainingSeats"))
        if cabin != "business" or points is None or points >= MAX_PROGRAM_POINTS:
            continue
        if PROGRAMS[source]["has_seat_count"] and (seats or 0) < 1:
            continue
        result.append(trip_summary(trip, source))
    result.sort(key=lambda x: (
        x["program_points"] if x["program_points"] is not None else 10**9,
        0 if x["stops"] in (None, 0) else 1,
        -(x["remaining_seats"] or 0),
    ))
    return result[:TRIPS_PER_AVAILABILITY]

def candidate_rank(s):
    mr = s["approx_amex_mr"]
    normalized = mr if mr is not None else s["program_points"]
    return (
        normalized if normalized is not None else 10**9,
        0 if s["direct"] else 1,
        -(s["remaining_seats"] or 0),
        s["date"] or "",
    )

def main():
    candidates = []

    # Broad worldwide search across all supported AmEx-transferable programs + United.
    for source in PROGRAMS:
        for region in DESTINATION_REGIONS:
            try:
                rows = paginate(
                    "/availability",
                    {
                        "source": source,
                        "cabin": "business",
                        "start_date": TODAY,
                        "end_date": END_DATE,
                        "origin_region": "North America",
                        "destination_region": region,
                        "min_cabin_pct": 100,
                    },
                )
            except Exception as exc:
                print(f"warning: {source}/{region} availability failed: {exc}", file=sys.stderr)
                continue
            for row in rows:
                if qualifies(row, source, region):
                    candidates.append(summary(row, source, region))

    dedup = {}
    for s in candidates:
        key = s["availability_id"] or (
            s["source"], s["origin"], s["destination"], s["date"], s["program_points"]
        )
        dedup[key] = s
    candidates = list(dedup.values())

    # Preserve breadth: one strong candidate per source/destination/month first,
    # then fill remaining detail budget with the next-best candidates.
    groups = defaultdict(list)
    for s in candidates:
        month = (s["date"] or "")[:7]
        groups[(s["source"], s["destination"], month)].append(s)
    for options in groups.values():
        options.sort(key=candidate_rank)

    representatives = []
    seconds = []
    for _, options in sorted(groups.items()):
        if options:
            representatives.append(options[0])
        if len(options) > 1:
            seconds.append(options[1])

    representatives.sort(key=candidate_rank)
    if len(representatives) > MAX_DETAIL_LOOKUPS:
        representatives = representatives[:MAX_DETAIL_LOOKUPS]
    else:
        remaining = MAX_DETAIL_LOOKUPS - len(representatives)
        seconds.sort(key=candidate_rank)
        representatives.extend(seconds[:remaining])

    ids = [(s["availability_id"], s["source"]) for s in representatives if s["availability_id"]]
    trip_map = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_to_key = {
            pool.submit(get_business_trips, aid, source): (aid, source)
            for aid, source in ids
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                trip_map[key] = future.result()
            except Exception as exc:
                trip_map[key] = []
                print(f"warning: trip lookup failed for {key}: {exc}", file=sys.stderr)

    for s in representatives:
        s["trips"] = trip_map.get((s["availability_id"], s["source"]), [])

    representatives.sort(key=lambda s: (
        s["date"] or "",
        candidate_rank(s),
        s["destination"],
        s["source"],
    ))

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Seats.aero cached availability",
        "criteria": {
            "origins": sorted(NYC),
            "start_date": TODAY,
            "end_date": END_DATE,
            "cabin": "business",
            "max_program_points_exclusive": MAX_PROGRAM_POINTS,
            "minimum_seats": 1,
            "min_cabin_pct": 100,
            "trip_focus": "solo hiking",
            "destination_regions": DESTINATION_REGIONS,
        },
        "programs": PROGRAMS,
        "raw_candidate_count": len(candidates),
        "detailed_candidate_count": len(representatives),
        "candidates": representatives,
        "notes": [
            "This uses Seats.aero cached award availability, not live airline search.",
            "AmEx MR equivalents are estimates based on the current standard transfer ratio and rounded up to the next 1,000 MR; verify the current transfer ratio before moving points.",
            "For programs where Seats.aero does not provide reliable seat counts, a zero or missing count does not necessarily mean zero seats.",
            "The ChatGPT alert layer evaluates hiking quality, seasonal trail conditions, access logistics, and trip length before notifying.",
            "Verify any promising award directly with the operating/loyalty program before transferring points or booking.",
        ],
    }

    path = Path("data/solo-hiking.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "raw_candidate_count": result["raw_candidate_count"],
        "detailed_candidate_count": result["detailed_candidate_count"],
        "programs": len(PROGRAMS),
        "output": str(path),
    }))

if __name__ == "__main__":
    main()
