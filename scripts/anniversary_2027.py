#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://seats.aero/partnerapi"
SOURCES = ["flyingblue", "aeroplan"]
MAX_POINTS = 100_000
MIN_SEATS = 2

NYC = {"JFK", "EWR", "LGA"}
UK_ARRIVALS = {"LHR", "LGW", "BRS"}

OUT_START = "2027-09-14"
OUT_END = "2027-09-16"
RETURN_START = "2027-10-02"
RETURN_END = "2027-10-04"

TAKE = 1000
MAX_RETURN_DETAIL_LOOKUPS_PER_SOURCE = 180

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
        q = dict(params)
        q["take"] = TAKE
        if cursor is not None:
            q["cursor"] = cursor
            q["skip"] = skip
        payload = api_get(path, q)
        batch = items(payload)
        for row in batch:
            rid = row.get("ID") or row.get("id")
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
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

def availability_summary(row, source):
    origin, destination = route_fields(row)
    return {
        "source": source,
        "availability_id": row.get("ID") or row.get("id"),
        "origin": origin,
        "destination": destination,
        "date": row.get("Date") or row.get("date"),
        "points": nint(row.get("JMileageCost")),
        "remaining_seats": nint(row.get("JRemainingSeats")),
        "airlines": row.get("JAirlines"),
        "direct": bool(row.get("JDirect")),
        "updated_at": row.get("UpdatedAt") or row.get("updatedAt"),
    }

def qualifies_summary(s):
    return (
        s["points"] is not None
        and s["points"] < MAX_POINTS
        and (s["remaining_seats"] or 0) >= MIN_SEATS
    )

def trip_summary(trip, source):
    segments = trip.get("AvailabilitySegments") or trip.get("availabilitySegments") or trip.get("segments") or []
    clean_segments = []
    for seg in segments:
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
        "source": source,
        "trip_id": trip.get("ID") or trip.get("id"),
        "cabin": trip.get("Cabin") or trip.get("cabin"),
        "points": nint(trip.get("MileageCost") or trip.get("mileageCost")),
        "remaining_seats": nint(trip.get("RemainingSeats") or trip.get("remainingSeats")),
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

def get_trips(availability_id, source):
    if not availability_id:
        return []
    payload = api_get(
        f"/trips/{urllib.parse.quote(str(availability_id))}",
        {"min_cabin_pct": 100},
    )
    result = []
    for trip in items(payload):
        cabin = str(trip.get("Cabin") or trip.get("cabin") or "").lower()
        points = nint(trip.get("MileageCost") or trip.get("mileageCost"))
        seats = nint(trip.get("RemainingSeats") or trip.get("remainingSeats"))
        if (
            cabin == "business"
            and points is not None
            and points < MAX_POINTS
            and (seats or 0) >= MIN_SEATS
        ):
            result.append(trip_summary(trip, source))
    result.sort(key=lambda x: (
        x["points"] if x["points"] is not None else 10**9,
        0 if x["stops"] in (None, 0) else 1,
        -(x["remaining_seats"] or 0),
    ))
    return result[:5]

def enrich(options):
    trip_map = {}
    pairs = [(o["availability_id"], o["source"]) for o in options if o.get("availability_id")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(get_trips, aid, source): (aid, source) for aid, source in pairs}
        for future in as_completed(future_map):
            aid, source = future_map[future]
            try:
                trip_map[(aid, source)] = future.result()
            except Exception as exc:
                print(f"warning: trip lookup failed for {source}/{aid}: {exc}", file=sys.stderr)
                trip_map[(aid, source)] = []
    out = []
    for option in options:
        x = dict(option)
        x["trips"] = trip_map.get((option.get("availability_id"), option["source"]), [])
        out.append(x)
    return out

def dedup(options):
    seen = {}
    for s in options:
        key = s["availability_id"] or (
            s["source"], s["origin"], s["destination"], s["date"], s["points"]
        )
        seen[key] = s
    return list(seen.values())

def main():
    outbound = []
    returns = []

    for source in SOURCES:
        out_rows = paginate("/search", {
            "origin_airport": ",".join(sorted(NYC)),
            "destination_airport": ",".join(sorted(UK_ARRIVALS)),
            "start_date": OUT_START,
            "end_date": OUT_END,
            "sources": source,
            "cabins": "business",
            "min_cabin_pct": 100,
            "order_by": "lowest_mileage",
        })
        for row in out_rows:
            s = availability_summary(row, source)
            if s["origin"] in NYC and s["destination"] in UK_ARRIVALS and qualifies_summary(s):
                outbound.append(s)

        return_rows = paginate("/availability", {
            "source": source,
            "cabin": "business",
            "start_date": RETURN_START,
            "end_date": RETURN_END,
            "origin_region": "Europe",
            "destination_region": "North America",
            "min_cabin_pct": 100,
        })
        source_returns = []
        for row in return_rows:
            s = availability_summary(row, source)
            if s["destination"] in NYC and qualifies_summary(s):
                source_returns.append(s)
        source_returns.sort(key=lambda x: (
            x["points"] if x["points"] is not None else 10**9,
            0 if x["direct"] else 1,
            -(x["remaining_seats"] or 0),
        ))
        returns.extend(source_returns[:MAX_RETURN_DETAIL_LOOKUPS_PER_SOURCE])

    outbound = dedup(outbound)
    returns = dedup(returns)

    outbound.sort(key=lambda x: (x["date"] or "", x["points"] or 10**9, 0 if x["direct"] else 1))
    returns.sort(key=lambda x: (x["date"] or "", x["points"] or 10**9, 0 if x["direct"] else 1))

    outbound = enrich(outbound)
    returns = enrich(returns)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Seats.aero cached availability",
        "trip": {
            "purpose": "2027 anniversary / Jane Austen Festival final weekend",
            "festival_dates": ["2027-09-10", "2027-09-19"],
            "target_festival_weekend": ["2027-09-17", "2027-09-19"],
            "anniversary": "2027-10-01",
            "uk_wish_list": [
                "Jane Austen Festival final weekend in Bath",
                "Lyme Park, the exterior of Pemberley in the BBC 1995 Pride and Prejudice",
                "Regency-style afternoon tea at Lyme if offered for 2027",
                "Sussex sparkling wine estate visit",
            ],
        },
        "criteria": {
            "programs": SOURCES,
            "cabin": "business",
            "max_points_exclusive": MAX_POINTS,
            "minimum_seats": MIN_SEATS,
            "min_cabin_pct": 100,
            "outbound_origins": sorted(NYC),
            "outbound_destinations": sorted(UK_ARRIVALS),
            "outbound_dates": [OUT_START, OUT_END],
            "return_origins": "any Europe airport represented in Seats.aero regional cache",
            "return_destinations": sorted(NYC),
            "return_dates": [RETURN_START, RETURN_END],
        },
        "outbound_options": outbound,
        "return_options": returns,
        "notes": [
            "The return is deliberately open-jaw: it can leave from a different European city after the UK portion.",
            "This watch requires at least two reported business-class seats in both directions.",
            "Seats.aero data is cached. Verify promising itineraries directly with the relevant loyalty program before transferring points or booking.",
            "October 2027 award inventory may not appear until it enters each program's booking horizon.",
        ],
    }

    path = Path("data/anniversary-2027.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "outbound_options": len(outbound),
        "return_options": len(returns),
        "output": str(path),
    }))

if __name__ == "__main__":
    main()
