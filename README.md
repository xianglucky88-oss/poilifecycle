# poilifecycle

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/poilifecycle`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## Location adjudication

Each observation's coordinates are compared with the POI's established
("anchored") location. A movement within the jitter radius keeps the anchor,
so a POI sitting on a grid line no longer flips cells on every collection,
while a larger movement, a closure or a reopen is still applied. Observations
without usable coordinates, out-of-order observations and same-day conflicts are
recorded as evidence but do not rewrite established history.

```text
make run                       # summary includes a "locations" block
make run_inc                   # incremental mode still adjudicates every observation
python -m poilifecycle.cli report          # inspect every POI
python -m poilifecycle.cli report <poi id> # inspect one POI
```

The report shows, per POI, the raw coordinates, the latest raw coordinates,
the final location/grid/status, the conclusion, its evidence (raw grid, stable
grid, distance to a grid line, distance moved, jitter radius, timestamp
relation) and the full observation and event history.

Tuning lives in `config.SPEC`: `grid_size`, `jitter_radius_m` and
`closed_statuses`. Adjudication state is stored in additional
`location_observations`, `location_state` and `location_events` tables; the
original `records`/`meta` tables are unchanged.
