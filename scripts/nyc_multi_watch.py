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
REGIONS = ["North America", "South America", "Europe", "Africa", "Asia", "Oceania"]
MAX_EFFECTIVE_COST = 100_000
TAKE = 1000
MAX_DETAIL_LOOKUPS = 220
TRIPS_PER_AVAILABILITY = 3
SOLO_END = "2026-12-31"

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

def qualifies(row, source, region):
    s = summary(row, source, region)
    if s["origin"] not in NYC or not s["destination"] or s["destination"] in NYC:
        return False
    if not s["business_available"] or not under_limit(s["program_points"], source, MAX_EFFECTIVE_COST):
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

def candidate_rank(s):
    return (
        s["effective_cost"] if s["effective_cost"] is not None else 10**9,
        0 if s["direct"] else 1,
        -(s["remaining_seats"] or 0),
        s["date"] or "",
    )

def has_one_seat(option):
    if any((t.get("remaining_seats") or 0) >= 1 for t in option.get("trips", [])):
        return True
    return not option["seat_count_reliable"] and bool(option.get("business_available"))

def has_two_seats(option):
    return any((t.get("remaining_seats") or 0) >= 2 for t in option.get("trips", []))

def main():
    candidates = []
    for source in PROGRAMS:
        for region in REGIONS:
            try:
                rows = paginate(
                    "/availability",
                    {
                        "source": source,
                        "cabin": "business",
                        "start_date": START_DATE,
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

    groups = defaultdict(list)
    for s in candidates:
        month = (s["date"] or "")[:7]
        groups[(s["destination"], month)].append(s)
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

    solo = [
        s for s in representatives
        if (s["date"] or "") <= SOLO_END and has_one_seat(s)
    ]
    couple_europe = [
        s for s in representatives
        if s["destination_region"] == "Europe" and has_two_seats(s)
    ]

    solo.sort(key=lambda s: (s["date"] or "", candidate_rank(s), s["destination"], s["source"]))
    couple_europe.sort(key=lambda s: (s["date"] or "", candidate_rank(s), s["destination"], s["source"]))

    shared_notes = [
        "Uses Seats.aero cached availability, not live airline search.",
        "AmEx MR equivalents use the current standard transfer ratio and are rounded up to the next 1,000 MR.",
        "United awards are compared using United miles because Membership Rewards do not transfer directly to United.",
        "Some current AmEx airline partners are not direct Seats.aero cached API sources and are not included in this feed.",
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
        "candidate_count": len(couple_europe),
        "candidates": couple_europe,
        "notes": shared_notes,
    }

    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/solo-hiking.json").write_text(json.dumps(solo_result, indent=2, sort_keys=True) + "\n")
    Path("data/couple-europe.json").write_text(json.dumps(couple_result, indent=2, sort_keys=True) + "\n")

    print(json.dumps({
        "raw_candidates": len(candidates),
        "detailed_candidates": len(representatives),
        "solo_hiking_candidates": len(solo),
        "couple_europe_two_seat_candidates": len(couple_europe),
    }))

if __name__ == "__main__":
    main()
