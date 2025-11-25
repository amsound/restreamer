# File: README.md
# hls2aac – Dockerized
## Build
docker compose build
## Run (mount your config)
# Place `stations.yaml` next to docker-compose.yml
docker compose up
## Test
# Stream one station (adts/flac/mp4/mpegts depend on your YAML)
curl -v http://localhost:8000/s/radio_3 > /dev/null
## Notes
- Change user agent via `UA` env if a provider is picky.
- `STATIONS_FILE` defaults to `/data/stations.yaml`; override in compose if needed.
