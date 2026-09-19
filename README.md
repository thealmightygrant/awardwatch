# awardwatch

Automated award-flight monitoring using Seats.aero cached availability.

## Architecture

GitHub Actions runs once each morning, queries Seats.aero using the repository secret `SEATS_AERO_API_KEY`, and publishes sanitized JSON into `data/`. ChatGPT scheduled tasks read those files and decide whether an itinerary is worth surfacing.

A Seats.aero rate-limit response fails the workflow before publishing, so the last good data remains in place rather than being replaced by a false empty result.

## Programs

The multi-program searches include United MileagePlus plus the AmEx Membership Rewards airline programs that Seats.aero exposes directly through its cached API:

- Aeromexico Rewards
- Air Canada Aeroplan
- Air France/KLM Flying Blue
- Avianca LifeMiles
- Delta SkyMiles
- Emirates Skywards
- JetBlue TrueBlue
- Qantas Frequent Flyer
- Qatar Airways Privilege Club
- Singapore KrisFlyer
- Virgin Atlantic Flying Club
- United MileagePlus

AmEx-equivalent costs are normalized using the current standard transfer ratio and rounded up to the next 1,000 Membership Rewards points. United is compared using United miles.

Some current AmEx airline partners are not available as direct Seats.aero cached API sources, so this is broad but not literally every AmEx transfer partner.

## Watches

### Solo hiking

`data/solo-hiking.json`

- NYC departure
- through 2026-12-31
- one business-class seat is sufficient
- effective one-way cost under 100,000 points
- worldwide candidates, with the ChatGPT layer filtering for genuinely good hiking timing

### Couple Europe

`data/couple-europe.json`

- NYC to Europe
- rolling one-year window
- at least two confirmed business-class seats
- effective one-way cost under 100,000 points per person
- alert layer strongly prioritizes holiday-friendly timing and experiential reasons to travel

### Couple New Year

`data/newyear-couple.json`

- DEN departure on 2026-12-28 or 2026-12-29
- return from the same destination to JFK/EWR/LGA on 2027-01-07 through 2027-01-10
- at least two confirmed business-class seats each way
- outbound and return may use different loyalty programs
- effective one-way cost under 100,000 points per person each way
- alert layer only surfaces warm/sunny New Year's destinations

## Running

Go to **Actions → Refresh award data → Run workflow**.

The workflow also runs daily at 11:30 UTC.

## Data limitations

Seats.aero API results are cached availability, not a live airline booking engine. Always verify a promising itinerary directly with the relevant loyalty program before transferring points or booking.
