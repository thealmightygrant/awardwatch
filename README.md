# awardwatch

Bridge for award-flight monitoring.

Current architecture:

1. A Cloudflare Worker calls the Seats.aero API using a Worker secret.
2. GitHub Actions fetches sanitized JSON from the Worker.
3. ChatGPT scheduled tasks read the JSON from this repository and alert only on matching award availability.

## Cloudflare Worker

Worker: `https://award-watch.agsherrick.workers.dev`

The Seats.aero API key stays in Cloudflare as `SEATS_AERO_API_KEY` and should never be committed here.

## Status

The initial workflow is a smoke test against `/test`. Once the real Worker endpoints are deployed, it will be replaced with the Aeroplan New Year's Eve and Flying Blue Europe refresh jobs.
