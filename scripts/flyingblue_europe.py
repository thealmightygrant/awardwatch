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

BASE = "https://seats.aero/partnerapi"
SOURCE = "flyingblue"
MAX_POINTS = 100_000
NYC = {"JFK", "EWR", "LGA"}
TAKE = 1000
MAX_DETAIL_LOOKUPS = 600
TRIPS_PER_AVAILABILITY = 4

today = datetime.now(timezone.utc).date()
START_DATE = today.isoformat()
END_DATE = (today + timedelta(days=365)).isoformat()

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
            if row_id and row_id in seen:
                continue
            if row_id:
                seen.add(row_id)
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

def summary(row):
    origin, destination = route_fields(row)
    return {
        "availability_id": row.get("ID") or row.get("id"),
        "origin": origin,
        "destination": destination,
        "date": row.get("Date") or row.get("date"),
        "business_available": bool(row.get("JAvailable")),
        "points": nint(row.get("JMileageCost")),
        "remaining_seats": nint(row.get("JRemainingSeats")),
        "airlines": row.get("JAirlines"),
        "direct": bool(row.get("JDirect")),
        "updated_at": row.get("UpdatedAt") or row.get("updatedAt"),
    }

def qualifies(row):
    s = summary(row)
    return (
        s["origin"] in NYC
        and s["business_available"]
        and s["points"] is not None
        and s["points"] < MAX_POINTS
        and (s["remaining_seats"] or 0) > 0
        and s["destination"]
    )

def trip_summary(trip):
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

def get_business_trips(availability_id):
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
            and (seats or 0) > 0
        ):
            result.append(trip_summary(trip))
    result.sort(
        key=lambda x: (
            x["points"] if x["points"] is not None else 10**9,
            0 if (x["stops"] in (None, 0)) else 1,
            -(x["remaining_seats"] or 0),
        )
    )
    return result[:TRIPS_PER_AVAILABILITY]

def option_rank(s):
    return (
        s["points"] if s["points"] is not None else 10**9,
        0 if s["direct"] else 1,
        -(s["remaining_seats"] or 0),
        s["date"] or "",
    )

def main():
    rows = paginate(
        "/availability",
        {
            "source": SOURCE,
            "cabin": "business",
            "start_date": START_DATE,
            "end_date": END_DATE,
            "origin_region": "North America",
            "destination_region": "Europe",
            "min_cabin_pct": 100,
        },
    )

    candidates = []
    for row in rows:
        if qualifies(row):
            candidates.append(summary(row))

    # De-duplicate availability records.
    dedup = {}
    for s in candidates:
        key = s["availability_id"] or (
            s["origin"],
            s["destination"],
            s["date"],
            s["points"],
        )
        dedup[key] = s
    candidates = list(dedup.values())

    # Preserve seasonal coverage: keep the best two options per destination/month.
    by_dest_month = defaultdict(list)
    for s in candidates:
        month = (s["date"] or "")[:7]
        by_dest_month[(s["destination"], month)].append(s)

    representatives = []
    for _, options in sorted(by_dest_month.items()):
        options.sort(key=option_rank)
        representatives.extend(options[:2])

    # Guarantee at least one representative per destination/month before adding seconds.
    if len(representatives) > MAX_DETAIL_LOOKUPS:
        firsts = []
        seconds = []
        for _, options in sorted(by_dest_month.items()):
            options.sort(key=option_rank)
            if options:
                firsts.append(options[0])
            if len(options) > 1:
                seconds.append(options[1])
        firsts.sort(key=option_rank)
        seconds.sort(key=option_rank)
        representatives = firsts[:MAX_DETAIL_LOOKUPS]
        remaining = MAX_DETAIL_LOOKUPS - len(representatives)
        if remaining > 0:
            representatives.extend(seconds[:remaining])

    # If there are still too many groups, fall back to best globally.
    representatives.sort(key=option_rank)
    representatives = representatives[:MAX_DETAIL_LOOKUPS]

    ids = [s["availability_id"] for s in representatives if s["availability_id"]]
    trip_map = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_to_id = {pool.submit(get_business_trips, aid): aid for aid in ids}
        for future in as_completed(future_to_id):
            aid = future_to_id[future]
            try:
                trip_map[aid] = future.result()
            except Exception as exc:
                trip_map[aid] = []
                print(f"warning: trip lookup failed for {aid}: {exc}", file=sys.stderr)

    destinations = defaultdict(list)
    for s in representatives:
        option = dict(s)
        option["trips"] = trip_map.get(s["availability_id"], [])
        destinations[s["destination"]].append(option)

    destination_list = []
    for airport, options in sorted(destinations.items()):
        options.sort(key=lambda x: (x["date"] or "", x["points"] or 10**9))
        destination_list.append({
            "airport": airport,
            "months": sorted({(o["date"] or "")[:7] for o in options}),
            "options": options,
        })

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Seats.aero cached Flying Blue availability",
        "criteria": {
            "origins": sorted(NYC),
            "destination_region": "Europe",
            "start_date": START_DATE,
            "end_date": END_DATE,
            "cabin": "business",
            "max_points_exclusive": MAX_POINTS,
            "min_cabin_pct": 100,
            "minimum_reported_seats": 1,
            "seasonal_sampling": "up to two cheapest representative dates per destination per calendar month, capped globally",
            "max_detailed_availability_records": MAX_DETAIL_LOOKUPS,
        },
        "raw_candidates_under_100k": len(candidates),
        "detailed_candidates": len(representatives),
        "destination_count": len(destination_list),
        "destinations": destination_list,
        "notes": [
            "This uses Seats.aero cached availability, not Flying Blue live search.",
            "The alert layer decides whether a date is actually a compelling time to visit based on weather, festivals, seasonal experiences, or other destination-specific reasons.",
            "Verify any promising itinerary directly with Air France/KLM Flying Blue before transferring points or booking.",
            "Seat counts are preserved; this search does not require two seats unless the alert layer is configured to do so.",
        ],
    }

    out_path = Path("data/flyingblue-europe.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "raw_candidates_under_100k": result["raw_candidates_under_100k"],
        "detailed_candidates": result["detailed_candidates"],
        "destination_count": result["destination_count"],
        "output": str(out_path),
    }))

if __name__ == "__main__":
    main()
