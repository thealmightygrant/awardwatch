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
SOURCE = "aeroplan"
MAX_POINTS = 100_000
ORIGIN = "DEN"
NYC = ["JFK", "EWR", "LGA"]
OUT_START = "2026-12-28"
OUT_END = "2026-12-29"
RETURN_START = "2027-01-07"
RETURN_END = "2027-01-10"
REGIONS = ["North America", "South America", "Africa", "Asia", "Europe", "Oceania"]
TAKE = 1000

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
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Seats.aero HTTP {e.code}: {body[:1000]}") from e
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

def paginate(path, params, max_pages=20):
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

def summary(row, region=None):
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
        "direct": row.get("JDirect"),
        "region": region,
        "updated_at": row.get("UpdatedAt") or row.get("updatedAt"),
    }

def qualifies_summary(row, required_origin=None):
    s = summary(row)
    if required_origin and s["origin"] != required_origin:
        return False
    return (
        s["business_available"]
        and s["points"] is not None
        and s["points"] < MAX_POINTS
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
    if not availability_id:
        return []
    payload = api_get(f"/trips/{urllib.parse.quote(str(availability_id))}", {"min_cabin_pct": 100})
    result = []
    for trip in items(payload):
        cabin = str(trip.get("Cabin") or trip.get("cabin") or "").lower()
        points = nint(trip.get("MileageCost") or trip.get("mileageCost"))
        if cabin == "business" and points is not None and points < MAX_POINTS:
            result.append(trip_summary(trip))
    result.sort(key=lambda x: (x["points"] if x["points"] is not None else 10**9, x["stops"] if x["stops"] is not None else 99))
    return result

def chunks(values, size):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]

def main():
    # Broad worldwide outbound scan, then narrow to DEN.
    outbound_rows = []
    for region in REGIONS:
        rows = paginate("/availability", {
            "source": SOURCE,
            "cabin": "business",
            "start_date": OUT_START,
            "end_date": OUT_END,
            "origin_region": "North America",
            "destination_region": region,
            "min_cabin_pct": 100,
        })
        for row in rows:
            if qualifies_summary(row, required_origin=ORIGIN):
                s = summary(row, region=region)
                outbound_rows.append((row, s))

    # Deduplicate summary rows.
    dedup_out = {}
    for row, s in outbound_rows:
        key = s["availability_id"] or (s["origin"], s["destination"], s["date"], s["points"])
        dedup_out[key] = (row, s)
    outbound_rows = list(dedup_out.values())

    destination_airports = sorted({s["destination"] for _, s in outbound_rows if s["destination"]})

    # Search returns from every outbound destination to NYC, in manageable chunks.
    return_rows = []
    for batch in chunks(destination_airports, 25):
        rows = paginate("/search", {
            "origin_airport": ",".join(batch),
            "destination_airport": ",".join(NYC),
            "start_date": RETURN_START,
            "end_date": RETURN_END,
            "sources": SOURCE,
            "cabins": "business",
            "min_cabin_pct": 100,
            "order_by": "lowest_mileage",
        })
        for row in rows:
            if qualifies_summary(row):
                s = summary(row)
                if s["origin"] in batch and s["destination"] in NYC:
                    return_rows.append((row, s))

    dedup_ret = {}
    for row, s in return_rows:
        key = s["availability_id"] or (s["origin"], s["destination"], s["date"], s["points"])
        dedup_ret[key] = (row, s)
    return_rows = list(dedup_ret.values())

    returns_by_origin = {}
    for row, s in return_rows:
        returns_by_origin.setdefault(s["origin"], []).append((row, s))

    matched_out = [(row, s) for row, s in outbound_rows if s["destination"] in returns_by_origin]
    matched_ids = {
        s["availability_id"]
        for row, s in matched_out
        if s["availability_id"]
    }
    for dest in {s["destination"] for _, s in matched_out}:
        for _, s in returns_by_origin.get(dest, []):
            if s["availability_id"]:
                matched_ids.add(s["availability_id"])

    trip_map = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_to_id = {pool.submit(get_business_trips, aid): aid for aid in matched_ids}
        for future in as_completed(future_to_id):
            aid = future_to_id[future]
            try:
                trip_map[aid] = future.result()
            except Exception as exc:
                trip_map[aid] = []
                print(f"warning: trips lookup failed for {aid}: {exc}", file=sys.stderr)

    destinations = []
    for dest in sorted({s["destination"] for _, s in matched_out}):
        outs = []
        for _, s in matched_out:
            if s["destination"] != dest:
                continue
            x = dict(s)
            x["trips"] = trip_map.get(s["availability_id"], [])
            outs.append(x)
        rets = []
        for _, s in returns_by_origin.get(dest, []):
            x = dict(s)
            x["trips"] = trip_map.get(s["availability_id"], [])
            rets.append(x)

        outs.sort(key=lambda x: (x["date"] or "", x["points"] if x["points"] is not None else 10**9))
        rets.sort(key=lambda x: (x["date"] or "", x["points"] if x["points"] is not None else 10**9))

        if outs and rets:
            destinations.append({
                "airport": dest,
                "outbound_options": outs,
                "return_options": rets,
            })

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Seats.aero cached Aeroplan availability",
        "criteria": {
            "outbound_origin": ORIGIN,
            "outbound_dates": [OUT_START, OUT_END],
            "return_airports": NYC,
            "return_dates": [RETURN_START, RETURN_END],
            "cabin": "business",
            "max_points_exclusive": MAX_POINTS,
            "min_cabin_pct": 100,
            "return_pairing": "same destination airport in v1",
        },
        "outbound_candidates_under_100k": len(outbound_rows),
        "destinations_with_matching_return": len(destinations),
        "destinations": destinations,
        "notes": [
            "This uses Seats.aero cached availability, not Aeroplan live search.",
            "Positive seat counts are reported when available; a zero seat count may mean unknown for Aeroplan in rare cases.",
            "Verify any promising itinerary directly on Air Canada before transferring points or booking.",
            "Warm/sunny suitability is intentionally not hard-coded here; the ChatGPT alert layer evaluates seasonality and trip quality.",
        ],
    }

    out_path = Path("data/aeroplan-nye.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "outbound_candidates_under_100k": result["outbound_candidates_under_100k"],
        "destinations_with_matching_return": result["destinations_with_matching_return"],
        "output": str(out_path),
    }))

if __name__ == "__main__":
    main()
