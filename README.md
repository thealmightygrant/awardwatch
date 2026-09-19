# awardwatch

Automated award-flight monitoring using Seats.aero data.

## Current architecture

1. GitHub Actions runs the search script once per day.
2. The Seats.aero API key is stored only as the GitHub Actions secret `SEATS_AERO_API_KEY`.
3. The script queries Seats.aero cached availability and writes sanitized JSON into `data/`.
4. ChatGPT scheduled tasks read that JSON and alert only on matching trips.

The Cloudflare Worker used during setup is no longer required for the steady-state workflow.

## Aeroplan New Year's Eve watch

`scripts/aeroplan_nye.py` searches:

- DEN outbound on 2026-12-28 or 2026-12-29
- Business class
- Under 100,000 Aeroplan points per person each way
- Return from the same destination airport to JFK/EWR/LGA on 2027-01-07 through 2027-01-10
- 100% business-cabin routing according to Seats.aero's `min_cabin_pct=100` filter

Results are written to:

`data/aeroplan-nye.json`

The script deliberately does not hard-code weather/seasonality. The alert layer evaluates whether each destination is genuinely sunny/warm and compelling over New Year's.

## Required secret

In GitHub:

**Settings → Secrets and variables → Actions → New repository secret**

Create:

- Name: `SEATS_AERO_API_KEY`
- Value: your Seats.aero Pro API key

Do not commit the key to the repository.

## Running

Go to **Actions → Refresh award data → Run workflow**.

The workflow also runs daily at 11:30 UTC.

## Data limitations

Seats.aero Pro API results are cached availability, not Aeroplan live-search results. Any promising itinerary should be verified directly with Air Canada before transferring points or booking.
