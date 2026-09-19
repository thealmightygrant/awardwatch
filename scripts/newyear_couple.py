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
from datetime import datetime, timezone
from pathlib import Path

from award_programs import PROGRAMS, approx_mr, effective_cost, under_limit

BASE = "https://seats.aero/partnerapi"
MAX_EFFECTIVE_COST = 100_000
ORIGIN = "DEN"
NYC = ["JFK", "EWR", "LGA"]
OUT_START = "2026-12-28"
OUT_END = "2026-12-29"
RETURN_START = "2027-01-07"
RETURN_END = "2027-01-10"
REGIONS = ["North America", "South America", "Europe", "Africa", "Asia", "Oceania"]
TAKE = 1000
TRIPS_PER_AVAILABILITY = 5

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

def paginate(path, params, max_pages=50):
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

def summary(row, source, region=None):
    origin, destination = route_fields(row)
    points = nint(row.get("JMileageCost"))
    seats = nint(row.get("JRemainingSeats"))
    return {
        "source": source,
        "program": PROGRAMS[source]["name"],
        "funding": PROGRAMS[source]["funding"],
        "origin": origin,
        "destination": destination,
        "date": row.get("Date") or row.get("date"),
        "destination_region": region,
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

def qualifies(row, source, required_origin=None):
    s = summary(row, source)
    if required_origin and s["origin"] != required_origin:
        return False
    if not s["business_available"] or not s["destination"]:
        return False
    if not under_limit(s["program_points"], source, MAX_EFFECTIVE_COST):
        return False
    if PROGRAMS[source]["has_seat_count"] and (s["remaining_seats"] or 0) < 2:
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

def get_two_seat_business_trips(availability_id, source):
    payload = api_get(
        f"/trips/{urllib.parse.quote(str(availability_id))}",
        {"min_cabin_pct": 100},
    )
    result = []
    for trip in items(payload):
        cabin = str(trip.get("Cabin") or trip.get("cabin") or "").lower()
        points = nint(trip.get("MileageCost") or trip.get("mileageCost"))
        seats = nint(trip.get("RemainingSeats") or trip.get("remainingSeats"))
        if cabin != "business":
            continue
        if not under_limit(points, source, MAX_EFFECTIVE_COST):
            continue
        if (seats or 0) < 2:
            continue
        result.append(trip_summary(trip, source))
    result.sort(key=lambda x: (
        x["effective_cost"] if x["effective_cost"] is not None else 10**9,
        0 if x["stops"] in (None, 0) else 1,
        -(x["remaining_seats"] or 0),
    ))
    return result[:TRIPS_PER_AVAILABILITY]

def chunks(values, size):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]

def enrich(options):
    trip_map = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_to_key = {}
        for s in options:
            aid = s.get("availability_id")
            if aid:
                key = (aid, s["source"])
                if key not in future_to_key.values():
                    future_to_key[pool.submit(get_two_seat_business_trips, aid, s["source"])] = key
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                trip_map[key] = future.result()
            except Exception as exc:
                trip_map[key] = []
                print(f"warning: trips lookup failed for {key}: {exc}", file=sys.stderr)

    enriched = []
    for s in options:
        x = dict(s)
        x["trips"] = trip_map.get((s.get("availability_id"), s["source"]), [])
        if x["trips"]:
            enriched.append(x)
    return enriched

def main():
    outbound = []

    # Narrow two-day DEN scan across all directly supported programs.
    for source in PROGRAMS:
        for region in REGIONS:
            try:
                rows = paginate(
                    "/availability",
                    {
                        "source": source,
                        "cabin": "business",
                        "start_date": OUT_START,
                        "end_date": OUT_END,
                        "origin_region": "North America",
                        "destination_region": region,
                        "min_cabin_pct": 100,
                    },
                )
            except Exception as exc:
                if "HTTP 429" in str(exc):
                    raise
                print(f"warning: outbound {source}/{region} failed: {exc}", file=sys.stderr)
                continue
            for row in rows:
                if qualifies(row, source, required_origin=ORIGIN):
                    s = summary(row, source, region=region)
                    outbound.append(s)

    dedup = {}
    for s in outbound:
        key = s["availability_id"] or (
            s["source"], s["origin"], s["destination"], s["date"], s["program_points"]
        )
        dedup[key] = s
    outbound = list(dedup.values())

    destinations = sorted({s["destination"] for s in outbound if s["destination"]})

    # Returns may be booked through a different program than the outbound.
    returns = []
    for source in PROGRAMS:
        for batch in chunks(destinations, 25):
            try:
                rows = paginate(
                    "/search",
                    {
                        "origin_airport": ",".join(batch),
                        "destination_airport": ",".join(NYC),
                        "start_date": RETURN_START,
                        "end_date": RETURN_END,
                        "sources": source,
                        "cabins": "business",
                        "min_cabin_pct": 100,
                        "order_by": "lowest_mileage",
                    },
                )
            except Exception as exc:
                if "HTTP 429" in str(exc):
                    raise
                print(f"warning: return {source}/{batch[:2]}... failed: {exc}", file=sys.stderr)
                continue
            for row in rows:
                if not qualifies(row, source):
                    continue
                s = summary(row, source)
                if s["origin"] in batch and s["destination"] in NYC:
                    returns.append(s)

    dedup = {}
    for s in returns:
        key = s["availability_id"] or (
            s["source"], s["origin"], s["destination"], s["date"], s["program_points"]
        )
        dedup[key] = s
    returns = list(dedup.values())

    return_origins = {s["origin"] for s in returns}
    outbound = [s for s in outbound if s["destination"] in return_origins]

    outbound = enrich(outbound)
    returns = enrich(returns)

    returns_by_origin = defaultdict(list)
    for s in returns:
        returns_by_origin[s["origin"]].append(s)

    matched_destinations = []
    for dest in sorted({s["destination"] for s in outbound}):
        outs = [s for s in outbound if s["destination"] == dest]
        rets = returns_by_origin.get(dest, [])
        if not outs or not rets:
            continue
        outs.sort(key=lambda x: (
            x["date"] or "",
            min((t["effective_cost"] for t in x["trips"]), default=10**9),
            x["source"],
        ))
        rets.sort(key=lambda x: (
            x["date"] or "",
            min((t["effective_cost"] for t in x["trips"]), default=10**9),
            x["source"],
        ))
        matched_destinations.append({
            "airport": dest,
            "outbound_options": outs,
            "return_options": rets,
        })

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Seats.aero cached multi-program availability",
        "criteria": {
            "outbound_origin": ORIGIN,
            "outbound_dates": [OUT_START, OUT_END],
            "return_airports": NYC,
            "return_dates": [RETURN_START, RETURN_END],
            "cabin": "business",
            "max_effective_cost_exclusive": MAX_EFFECTIVE_COST,
            "minimum_confirmed_seats": 2,
            "min_cabin_pct": 100,
            "return_pairing": "same destination airport; outbound and return may use different loyalty programs",
        },
        "programs": PROGRAMS,
        "outbound_candidates_with_two_seat_trip_data": len(outbound),
        "destinations_with_matching_two_seat_return": len(matched_destinations),
        "destinations": matched_destinations,
        "notes": [
            "Uses Seats.aero cached availability, not live airline search.",
            "For AmEx-transferable programs, the <100k threshold is applied to approximate Membership Rewards required after the standard transfer ratio; United uses United miles.",
            "Both directions require trip-level data showing at least two business-class seats.",
            "Some current AmEx airline partners are not direct Seats.aero cached API sources and are not included in this feed.",
            "Verify promising itineraries directly with the loyalty program before transferring points or booking.",
        ],
    }

    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/newyear-couple.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "outbound_two_seat_candidates": result["outbound_candidates_with_two_seat_trip_data"],
        "matched_destinations": result["destinations_with_matching_two_seat_return"],
        "output": "data/newyear-couple.json",
    }))

if __name__ == "__main__":
    main()
