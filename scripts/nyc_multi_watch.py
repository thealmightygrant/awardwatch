#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from award_programs import PROGRAMS, approx_mr, effective_cost, under_limit

BASE = "https://seats.aero/partnerapi"
NYC = {"JFK", "EWR", "LGA"}
MAX_EFFECTIVE_COST = 100_000
TAKE = 1000
MAX_COUPLE_DETAILS = 12
MAX_SOLO_DETAILS = 12
TRIPS_PER_AVAILABILITY = 3
SOLO_END = "2026-12-31"

# Instead of scanning every world region for every program, reuse one NYC->Europe
# snapshot for both watches and add one compact search for non-Europe hiking
# gateways. This keeps API traffic bounded while preserving the intended use.
NON_EUROPE_HIKING_GATEWAYS = {
    "YYC", "YVR", "SCL", "LIM", "UIO", "NRT", "HND", "KIX", "AKL", "CHC"
}

TODAY_DATE = datetime.now(timezone.utc).date()
START_DATE = TODAY_DATE.isoformat()
END_DATE = (TODAY_DATE + timedelta(days=365)).isoformat()

API_KEY = os.environ.get("SEATS_AERO_API_KEY")
if not API_KEY:
    raise SystemExit("SEATS_AERO_API_KEY is not set")

def api_get(path, params=None, retries=3):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Partner-Authorization": API_KEY,
            "Accept": "application/json",
            "User-Agent": "awardwatch/2.0",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"Seats.aero HTTP {e.code}: {body[:1200]}") from e
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(3 * (attempt + 1))

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

def paginate(path, params, max_pages=40):
    out = []
    cursor = None
    skip = 0
    seen = set()
    for _ in range(max_pages):
        q = dict(params)
        q["take"] = TAKE
        if cursor is not None:
            q["cursor"] = cursor
            q["skip"] = skip
        payload = api_get(path, q)
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
        "approx_amex_mr": approx_mr(points, source),
        "effective_cost": effective_cost(points, source),
        "remaining_seats": seats,
        "seat_count_reliable": PROGRAMS[source]["has_seat_count"],
        "airlines": row.get("JAirlines"),
        "direct": bool(row.get("JDirect")),
        "updated_at": row.get("UpdatedAt") or row.get("updatedAt"),
    }

def qualifies_summary(s):
    if s["origin"] not in NYC or not s["destination"] or s["destination"] in NYC:
        return False
    if not s["business_available"] or not under_limit(s["program_points"], s["source"], MAX_EFFECTIVE_COST):
        return False
    if s["seat_count_reliable"] and (s["remaining_seats"] or 0) < 1:
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
        "approx_amex_mr": approx_mr(points, source),
        "effective_cost": effective_cost(points, source),
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
        if cabin != "business" or not under_limit(points, source, MAX_EFFECTIVE_COST):
            continue
        if PROGRAMS[source]["has_seat_count"] and (seats or 0) < 1:
            continue
        result.append(trip_summary(trip, source))
    result.sort(key=lambda x: (
        x["effective_cost"] if x["effective_cost"] is not None else 10**9,
        0 if x["stops"] in (None, 0) else 1,
        -(x["remaining_seats"] or 0),
    ))
    return result[:TRIPS_PER_AVAILABILITY]

def rank(s):
    return (
        s["effective_cost"] if s["effective_cost"] is not None else 10**9,
        0 if s["direct"] else 1,
        -(s["remaining_seats"] or 0),
        s["date"] or "",
    )

def representatives(options, limit):
    groups = defaultdict(list)
    for s in options:
        groups[(s["destination"], (s["date"] or "")[:7])].append(s)
    reps = []
    for values in groups.values():
        values.sort(key=rank)
        reps.append(values[0])
    reps.sort(key=rank)
    return reps[:limit]

def main():
    candidates = []

    for source in PROGRAMS:
        # Shared NYC -> Europe snapshot, used by both couple-Europe and solo hiking.
        try:
            rows = paginate(
                "/availability",
                {
                    "source": source,
                    "cabin": "business",
                    "start_date": START_DATE,
                    "end_date": END_DATE,
                    "origin_region": "North America",
                    "destination_region": "Europe",
                    "min_cabin_pct": 100,
                },
            )
            for row in rows:
                s = summary(row, source, "Europe")
                if qualifies_summary(s):
                    candidates.append(s)
        except Exception as exc:
            print(f"warning: {source}/Europe availability failed: {exc}", file=sys.stderr)

        # Compact targeted worldwide coverage for the solo hiking watch.
        try:
            rows = paginate(
                "/search",
                {
                    "origin_airport": ",".join(sorted(NYC)),
                    "destination_airport": ",".join(sorted(NON_EUROPE_HIKING_GATEWAYS)),
                    "start_date": START_DATE,
                    "end_date": SOLO_END,
                    "sources": source,
                    "cabins": "business",
                    "min_cabin_pct": 100,
                    "order_by": "lowest_mileage",
                },
                max_pages=10,
            )
            for row in rows:
                s = summary(row, source, "targeted-non-Europe-hiking")
                if qualifies_summary(s):
                    candidates.append(s)
        except Exception as exc:
            print(f"warning: {source}/hiking search failed: {exc}", file=sys.stderr)

    dedup = {}
    for s in candidates:
        key = s["availability_id"] or (
            s["source"], s["origin"], s["destination"], s["date"], s["program_points"]
        )
        dedup[key] = s
    candidates = list(dedup.values())

    solo_pool = [
        s for s in candidates
        if (s["date"] or "") <= SOLO_END
    ]
    couple_pool = [
        s for s in candidates
        if s["destination_region"] == "Europe"
        and s["seat_count_reliable"]
        and (s["remaining_seats"] or 0) >= 2
    ]

    solo_reps = representatives(solo_pool, MAX_SOLO_DETAILS)
    couple_reps = representatives(couple_pool, MAX_COUPLE_DETAILS)

    detail_targets = {}
    for s in solo_reps + couple_reps:
        if s.get("availability_id"):
            detail_targets[(s["availability_id"], s["source"])] = s

    trip_map = {}
    # Keep concurrency low to avoid Seats.aero burst-rate limits.
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_to_key = {
            pool.submit(get_business_trips, aid, source): (aid, source)
            for aid, source in detail_targets
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                trip_map[key] = future.result()
            except Exception as exc:
                trip_map[key] = []
                print(f"warning: trip lookup failed for {key}: {exc}", file=sys.stderr)

    def attach(options):
        out = []
        for s in options:
            x = dict(s)
            x["trips"] = trip_map.get((s.get("availability_id"), s["source"]), [])
            out.append(x)
        return out

    solo = attach(solo_reps)
    couple = attach(couple_reps)

    # Couple alerts require confirmed trip-level two-seat availability.
    couple = [
        s for s in couple
        if any((t.get("remaining_seats") or 0) >= 2 for t in s.get("trips", []))
    ]

    shared_notes = [
        "Uses Seats.aero cached availability, not live airline search.",
        "The scanner reuses one NYC-to-Europe summary snapshot for both watches and performs at most 24 trip-detail lookups total.",
        "Non-Europe solo-hiking coverage is limited to a targeted gateway list rather than a full worldwide regional crawl.",
        "AmEx MR equivalents use the current standard transfer ratio and are rounded up to the next 1,000 MR.",
        "United awards are compared using United miles because Membership Rewards do not transfer directly to United.",
        "Verify any promising award directly with the loyalty program before transferring points or booking.",
    ]

    solo_result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "origins": sorted(NYC),
            "start_date": START_DATE,
            "end_date": SOLO_END,
            "cabin": "business",
            "max_effective_cost_exclusive": MAX_EFFECTIVE_COST,
            "minimum_seats": 1,
            "trip_focus": "solo hiking",
            "non_europe_hiking_gateways": sorted(NON_EUROPE_HIKING_GATEWAYS),
        },
        "programs": PROGRAMS,
        "candidate_count": len(solo),
        "candidates": solo,
        "notes": shared_notes,
    }

    couple_result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "origins": sorted(NYC),
            "destination_region": "Europe",
            "start_date": START_DATE,
            "end_date": END_DATE,
            "cabin": "business",
            "max_effective_cost_exclusive": MAX_EFFECTIVE_COST,
            "minimum_confirmed_seats": 2,
            "trip_focus": "couple experiential Europe",
        },
        "programs": PROGRAMS,
        "candidate_count": len(couple),
        "candidates": couple,
        "notes": shared_notes,
    }

    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/solo-hiking.json").write_text(json.dumps(solo_result, indent=2, sort_keys=True) + "\n")
    Path("data/couple-europe.json").write_text(json.dumps(couple_result, indent=2, sort_keys=True) + "\n")

    print(json.dumps({
        "summary_candidates": len(candidates),
        "solo_detail_lookups": len(solo_reps),
        "couple_detail_lookups": len(couple_reps),
        "unique_detail_lookups": len(detail_targets),
        "solo_hiking_candidates": len(solo),
        "couple_europe_two_seat_candidates": len(couple),
    }))

if __name__ == "__main__":
    main()
