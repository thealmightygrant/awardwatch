# awardwatch

Automated award-flight monitoring using Seats.aero cached award data.

## Architecture

1. GitHub Actions runs the award-search scripts daily.
2. The Seats.aero API key is stored only as the GitHub Actions secret `SEATS_AERO_API_KEY`.
3. The scripts query Seats.aero and write sanitized JSON into `data/`.
4. ChatGPT scheduled tasks read those JSON files and notify only when a result satisfies the trip criteria.

The Cloudflare Worker used during setup is no longer required for the steady-state workflow.

## Searches

### Aeroplan New Year's Eve

`scripts/aeroplan_nye.py`

- DEN outbound on 2026-12-28 or 2026-12-29
- Return to JFK/EWR/LGA on 2027-01-07 through 2027-01-10
- Business class
- Under 100,000 Aeroplan points per person each way
- 100% business-cabin routing according to Seats.aero's `min_cabin_pct=100`
- The ChatGPT alert layer filters the matching destinations for genuinely warm/sunny New Year's trips

Output: `data/aeroplan-nye.json`

### Flying Blue Europe

`scripts/flyingblue_europe.py`

- JFK/EWR/LGA to Europe
- Rolling one-year search window
- Business class
- Under 100,000 Flying Blue miles per person one way
- At least one reported award seat
- 100% business-cabin routing according to Seats.aero's `min_cabin_pct=100`
- Keeps representative award dates across destination/month combinations so the ChatGPT alert layer can evaluate festivals, harvests, northern-lights opportunities, hiking seasons, Christmas markets, and other destination-specific timing

Output: `data/flyingblue-europe.json`

## Required secret

In GitHub:

**Settings → Secrets and variables → Actions → New repository secret**

Create:

- Name: `SEATS_AERO_API_KEY`
- Value: your Seats.aero Pro API key

Do not commit the key to the repository.

## Running

Go to **Actions → Refresh award data → Run workflow**.

The workflow also runs daily at 11:30 UTC. It uses a concurrency lock and publishes generated files on top of the latest `main` branch to avoid races with overlapping runs.

## Data limitations

Seats.aero Pro API results are cached availability, not live airline-search results. Any promising itinerary should be verified directly with Air Canada or Air France/KLM before transferring points or booking.
