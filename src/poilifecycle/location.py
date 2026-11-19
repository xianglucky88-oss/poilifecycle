
"""Location-change adjudication for POI observations.

The source occasionally reports coordinates that sit right on a grid line.
A rounding error of a few metres then makes the derived grid cell flip on
every collection.  Treating every raw grid change as a relocation produces
constant false movements, but simply ignoring grid changes would hide real
relocations, closures and duplicate/missing reports.

This module keeps a per-POI *anchored* location and classifies each incoming
observation with one of the following conclusions:

``new``                 first usable coordinate for the POI
``unchanged``           coordinates stay in the anchored grid / within jitter
``jitter``              coordinates moved only within the jitter radius but
                        the *raw* grid cell changed (boundary flip); the
                        anchored grid is deliberately kept
``moved``               movement beyond the jitter radius; anchored location
                        and grid are updated
``missing``             no usable coordinate; the established location is
                        retained
``closed``              a closing status (e.g. ``withdrawn``/``closed``)
``reopened``            a non-closing observation after a closure
``backfilled``          an out-of-order observation that agrees with the
                        established location; it is recorded but does not
                        rewrite history
``late``                an out-of-order observation that *would* change the
                        established location/closure; recorded, not applied
``same_date_conflict``  an observation on the same calendar day as the
                        authoritative state but a different location; not
                        applied (the first observation of the day wins)
``undated``             an observation without a timestamp that would change
                        the established state; recorded, not applied
``duplicate``           exact location observation already processed (the
                        previous decision is returned, no new event)

Every decision carries ``evidence`` with the raw coordinates, the parsed
coordinates, both the raw and the stable grid, the distance to the anchor,
the distance to the nearest grid line, the jitter radius and the timestamp
relationship to the established state.  Decisions are persisted in the
``location_*`` tables together with the observation and the resulting state in
one transaction, which makes duplicate processing and crash recovery
idempotent.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from .config import SPEC

# Grid cell size in degrees (about 111 m of latitude at the equator).
GRID_SIZE = float(getattr(SPEC, "grid_size", 0.001))
# Movements at or below this radius are considered positioning jitter.
JITTER_RADIUS_M = float(getattr(SPEC, "jitter_radius_m", 30.0))
# Statuses that mean the POI is no longer operating.
CLOSED_STATUSES = frozenset(
    getattr(SPEC, "closed_statuses", ("closed", "withdrawn", "inactive", "deleted"))
)
EARTH_RADIUS_M = 6371008.8
_METERS_PER_DEGREE = 111320.0


# --------------------------------------------------------------------------- #
# Small parsing helpers.
# --------------------------------------------------------------------------- #
def _coord(value, limit):
    """Return a finite float in ``[-limit, limit]`` or ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in {"none", "null", "nan", "n/a", "-"}:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > limit:
        return None
    return number


def point(record):
    """Return ``(lat, lng)`` when both coordinates are usable, else ``None``."""
    lat = _coord(record.get("lat"), 90.0)
    lng = _coord(record.get("lng"), 180.0)
    if lat is None or lng is None:
        return None
    return lat, lng


def _ts_key(value):
    """Sortable timestamp key.

    Rank 0 means "no timestamp", rank 1 is an opaque textual timestamp and
    rank 2 is a parsed ISO-8601 / numeric timestamp.  A parsed date therefore
    takes precedence over an unparsable one, which keeps decisions
    deterministic when a source mixes formats.
    """
    if value is None:
        return (0, 0.0)
    if isinstance(value, bool):
        return (1, str(value))
    if isinstance(value, (int, float)):
        return (2, float(value))
    text = str(value).strip()
    if not text:
        return (0, 0.0)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (2, parsed.timestamp())
    except (ValueError, OverflowError):
        pass
    try:
        return (2, float(text))
    except (TypeError, ValueError):
        return (1, text)


def _ts_max(left, right):
    return left if _ts_key(left) >= _ts_key(right) else right


def _order_key(value):
    """Deterministic event-time order for a batch.

    Parseable timestamps come first in chronological order, then unparsable
    textual timestamps, then observations without a timestamp.  Undated
    observations are processed last so they can never anchor the state ahead
    of a dated observation that happens to arrive in the same batch.
    """
    key = _ts_key(value)
    if key[0] == 2:
        return (0, key[1])
    if key[0] == 1:
        return (1, key[1])
    return (2, 0.0)


def _day(value):
    """UTC calendar day of a timestamp, or ``None`` when unparsable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (ValueError, OverflowError):
        return None


def _relation(observed_ts, established_ts):
    # An observation without a timestamp is always "unknown": it can never be
    # proven newer than the established state.
    if not _ts_key(observed_ts)[0]:
        return "unknown"
    if not _ts_key(established_ts)[0]:
        return "new"
    left, right = _ts_key(observed_ts), _ts_key(established_ts)
    if left > right:
        return "newer"
    if left == right:
        return "same"
    return "older"


def poi_key(record):
    """Stable POI identity: ``id`` first, then ``name``.

    A record with neither falls back to a coordinate-based key rather than
    raising, so partially-populated rows can still be adjudicated.
    """
    for field in ("id", "name"):
        value = record.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    coords = point(record)
    if coords is not None:
        return "anon:%.6f,%.6f" % coords
    return "anon:" + json.dumps(
        [str(record.get("ts", "")), str(record.get("status", ""))],
        ensure_ascii=False)


def identity(record):
    """Stable, coordinate-normalised identity used for observation dedup.

    Only location-relevant fields take part, so kernel/derived columns and
    textual formatting of numbers do not create a second observation when old
    payloads are replayed.
    """
    coords = point(record)
    if coords is None:
        lat = _raw_location_value(record.get("lat"))
        lng = _raw_location_value(record.get("lng"))
    else:
        lat, lng = coords
    status = record.get("status")
    if isinstance(status, str):
        status = status.strip().lower()
    return {
        "id": poi_key(record),
        "ts": "" if record.get("ts") is None else str(record.get("ts")).strip(),
        "lat": lat,
        "lng": lng,
        "status": status,
    }


def _raw_location_value(value):
    if value is None:
        return None
    return str(value).strip()


def location_hash(record):
    blob = json.dumps(identity(record), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


# --------------------------------------------------------------------------- #
# Geometry.
# --------------------------------------------------------------------------- #
def grid_indices(lat, lng):
    """Grid cell indices for a coordinate pair.

    ``round`` before ``floor`` absorbs the float representation error that
    would otherwise map ``31.230000`` to the cell below its grid line.
    """
    return (math.floor(round(lat / GRID_SIZE, 9)),
            math.floor(round(lng / GRID_SIZE, 9)))


def grid_code(lat, lng):
    """Grid cell label for a coordinate pair."""
    lat_idx, lng_idx = grid_indices(lat, lng)
    return "LC:%d:%d" % (lat_idx, lng_idx)


def distance_m(a, b):
    """Great-circle distance in metres between two ``(lat, lng)`` points."""
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def boundary_distance_m(lat, lng):
    """Distance from a point to the nearest grid line, in metres.

    Points close to zero are exactly the ones whose grid label can flip under
    a tiny coordinate error.
    """
    lat_m = abs(lat / GRID_SIZE - round(lat / GRID_SIZE)) * GRID_SIZE * _METERS_PER_DEGREE
    lng_scale = _METERS_PER_DEGREE * max(math.cos(math.radians(lat)), 1e-9)
    lng_m = abs(lng / GRID_SIZE - round(lng / GRID_SIZE)) * GRID_SIZE * lng_scale
    return min(lat_m, lng_m)


# --------------------------------------------------------------------------- #
# Decision.
# --------------------------------------------------------------------------- #
def _evidence(record, state, has_coords, coords, relation, reason):
    anchor = None
    if state.get("lat") is not None and state.get("lng") is not None:
        anchor = (state["lat"], state["lng"])
    raw_grid = grid_code(*coords) if has_coords else None
    dist = distance_m(anchor, coords) if anchor and has_coords else None
    near = boundary_distance_m(*coords) if has_coords else None
    return {
        "poi": poi_key(record),
        "raw_lat": record.get("lat"),
        "raw_lng": record.get("lng"),
        "lat": coords[0] if has_coords else None,
        "lng": coords[1] if has_coords else None,
        "has_coordinates": has_coords,
        "raw_grid": raw_grid,
        "stable_grid": state.get("grid"),
        "anchor": ({"lat": anchor[0], "lng": anchor[1],
                    "ts": state.get("ts"), "grid": state.get("grid")}
                   if anchor else None),
        "distance_m": round(dist, 3) if dist is not None else None,
        "jitter_radius_m": JITTER_RADIUS_M,
        "boundary_proximity_m": round(near, 3) if near is not None else None,
        "grid_size_deg": GRID_SIZE,
        "status": record.get("status"),
        "closed_status": str(record.get("status") or "").strip().lower()
        in CLOSED_STATUSES,
        "ts": record.get("ts"),
        "state_ts": state.get("ts"),
        "last_seen_ts": state.get("last_seen_ts"),
        "ts_relation": relation,
        "reason": reason,
    }


def decide(record, state):
    """Classify one observation against the established per-POI state.

    Returns ``(decision, next_state)``.  ``decision`` has ``conclusion``,
    ``applied`` (the authoritative location/closure changed), ``reason`` and
    ``evidence``.
    """
    state = dict(state or {})
    coords = point(record)
    has_coords = coords is not None
    relation = _relation(record.get("ts"), state.get("last_seen_ts"))
    status = record.get("status")
    status_text = str(status).strip().lower() if status is not None else ""
    closed = bool(status_text) and status_text in CLOSED_STATUSES
    anchor = (state.get("lat"), state.get("lng")) \
        if state.get("lat") is not None and state.get("lng") is not None else None
    established = anchor is not None

    conclusion, applied, reason = _classify(
        record, state, coords, anchor, relation, closed, status_text)

    evidence = _evidence(record, state, has_coords, coords, relation, reason)

    new_state = dict(state)
    new_state.setdefault("poi", poi_key(record))
    new_state.setdefault("lat", None)
    new_state.setdefault("lng", None)
    new_state.setdefault("grid", None)
    new_state.setdefault("ts", None)
    new_state.setdefault("status", None)
    new_state.setdefault("open", True)
    new_state.setdefault("observations", 0)
    new_state.setdefault("last_seen_ts", None)
    new_state.setdefault("conclusion", None)
    new_state.setdefault("raw_lat", None)
    new_state.setdefault("raw_lng", None)
    new_state.setdefault("latest_raw_lat", None)
    new_state.setdefault("latest_raw_lng", None)
    new_state.setdefault("evidence", None)
    new_state.setdefault("final_conclusion", None)
    new_state.setdefault("final_evidence", None)
    new_state["name"] = record.get("name") or new_state.get("name")
    new_state["observations"] = int(new_state.get("observations") or 0) + 1
    new_state["latest_raw_lat"] = record.get("lat")
    new_state["latest_raw_lng"] = record.get("lng")
    if has_coords or closed:
        # Only observations that actually assert a location (or a lifecycle
        # state) take part in event-time ordering.  A report without usable
        # coordinates must not make later, genuinely older observations look
        # late.
        new_state["last_seen_ts"] = _ts_max(new_state.get("last_seen_ts"),
                                            record.get("ts"))
    new_state["conclusion"] = conclusion
    # ``evidence`` always describes the most recent observation; ``final_*``
    # stays paired with the authoritative (final) location so the report can
    # show both "what was last seen" and "what the conclusion is based on".
    new_state["evidence"] = evidence

    if applied:
        # The authoritative location / open flag changed.  A movement that is
        # still within the jitter radius (typical for a reopened POI that never
        # really moved) keeps the established anchor and therefore the stable
        # grid; otherwise the anchor is replaced.
        if has_coords:
            within = established and distance_m(anchor, coords) <= JITTER_RADIUS_M
            if not within:
                new_state["lat"], new_state["lng"] = coords
                new_state["grid"] = grid_code(*coords)
            new_state["raw_lat"] = record.get("lat")
            new_state["raw_lng"] = record.get("lng")
        new_state["open"] = not closed
        new_state["ts"] = record.get("ts")
        new_state["final_conclusion"] = conclusion
        new_state["final_evidence"] = evidence
        if status_text:
            new_state["status"] = status_text
    elif relation not in ("older", "same") and conclusion != "late":
        # Fresh, non-conflicting observations may update descriptive status
        # without touching the anchored location.
        if status_text:
            new_state["status"] = status_text

    return {
        "poi": poi_key(record),
        "conclusion": conclusion,
        "applied": bool(applied),
        "reason": reason,
        "evidence": evidence,
    }, new_state


def _classify(record, state, coords, anchor, relation, closed, status_text):
    """Return ``(conclusion, applied, reason)``."""
    has_coords = coords is not None
    established = anchor is not None

    # --- Established closed POI ------------------------------------------ #
    if state.get("open") is False:
        if relation == "older":
            return ("late", False,
                    "observation predates the closing event; not applied")
        if relation == "same":
            return ("same_date_conflict", False,
                    "observation has the same timestamp as the closing event")
        if relation == "unknown":
            return ("undated", False,
                    "observation has no timestamp and cannot reopen a closed POI")
        if closed:
            return ("closed", False, "POI is already closed")
        if not has_coords:
            return ("reopened", True,
                    "non-closing observation after closure without coordinates; "
                    "reopened, location retained")
        if distance_m(anchor, coords) > JITTER_RADIUS_M:
            return ("reopened", True,
                    "non-closing observation after closure at a new location; "
                    "reopened and relocated")
        return ("reopened", True,
                "non-closing observation after closure; established location retained")

    # --- No usable coordinate ------------------------------------------- #
    if not has_coords and not closed:
        return ("missing", False,
                "observation has no usable coordinates; established location retained")

    # --- Observation without a timestamp --------------------------------- #
    # It cannot be ordered against the established state, so it is recorded
    # as evidence but never allowed to overwrite a dated conclusion.
    if relation == "unknown" and established:
        if closed:
            return ("undated", False,
                    "closure has no timestamp and cannot supersede the dated state")
        if distance_m(anchor, coords) <= JITTER_RADIUS_M:
            same_grid = grid_code(*coords) == state.get("grid")
            return ("unchanged" if same_grid else "jitter", False,
                    "undated observation within the jitter radius")
        return ("undated", False,
                "observation has no timestamp; established location retained")

    # --- Closing status for an open POI ---------------------------------- #
    if closed:
        if relation == "older":
            return ("late", False,
                    "closing observation predates the established state; "
                    "kept for evidence but not applied")
        if relation == "same":
            return ("same_date_conflict", False,
                    "closure has the same timestamp as the established state")
        return ("closed", True, "closing status applied")

    # --- No established coordinate yet ----------------------------------- #
    if not established:
        if not has_coords:
            return ("missing", False,
                    "no usable coordinates and no established location")
        if relation == "older":
            return ("new", True,
                    "first usable coordinate for the POI (backfilled observation)")
        return ("new", True, "first usable coordinate for the POI")

    # --- Established location, usable coordinate ------------------------- #
    dist = distance_m(anchor, coords)
    if relation == "older":
        if dist <= JITTER_RADIUS_M:
            return ("backfilled", False,
                    "out-of-order observation matches the established location")
        return ("late", False,
                "out-of-order observation would move the established location; "
                "kept for evidence but not applied")

    if relation == "same":
        if dist > JITTER_RADIUS_M:
            return ("same_date_conflict", False,
                    "conflicting location with the same timestamp; "
                    "established location retained")
        return ("jitter" if grid_code(*coords) != state.get("grid") else "unchanged",
                False, "same-date observation within jitter radius")

    if dist <= JITTER_RADIUS_M:
        if grid_code(*coords) != state.get("grid"):
            return ("jitter", False,
                    "raw grid changed but movement is within the jitter radius; "
                    "stable grid retained")
        return ("unchanged", False, "movement within the jitter radius")

    # A second, different location on the same calendar day as the
    # authoritative observation is a conflicting report, not a second move.
    # Later days are allowed to relocate the POI.
    observed_day, anchored_day = _day(record.get("ts")), _day(state.get("ts"))
    if observed_day is not None and observed_day == anchored_day:
        return ("same_date_conflict", False,
                "same-day relocation conflicts with the established location; "
                "the first observation of the day wins")

    return ("moved", True,
            "movement %.1f m exceeds the jitter radius %.1f m; location updated"
            % (dist, JITTER_RADIUS_M))


# --------------------------------------------------------------------------- #
# Persistence-facing adjudicator.
# --------------------------------------------------------------------------- #
class Adjudicator:
    """Applies observations to a :class:`poilifecycle.store.Store`."""

    def __init__(self, store):
        self.store = store

    def apply(self, record):
        content_hash = location_hash(record)
        existing = self.store.location_observation(content_hash)
        if existing:
            return self._duplicate(existing, content_hash)

        state = self.store.location_state(poi_key(record)) or {}
        decision, next_state = decide(record, state)
        decision["content_hash"] = content_hash
        decision["duplicate"] = False
        decision["ts"] = record.get("ts")
        decision["status"] = record.get("status")
        decision["raw_lat"] = record.get("lat")
        decision["raw_lng"] = record.get("lng")
        inserted = self.store.apply_location(
            content_hash, poi_key(record), record, decision, next_state)
        if not inserted:
            existing = self.store.location_observation(content_hash)
            if existing:
                return self._duplicate(existing, content_hash)
        return decision

    @staticmethod
    def _duplicate(existing, content_hash):
        """Replayed observation: nothing changed, previous decision returned."""
        return {
            "poi": existing["poi"],
            "conclusion": "duplicate",
            "applied": False,
            "previously_applied": bool(existing.get("applied")),
            "reason": "exact observation already processed; previous decision: %s"
                      % existing.get("conclusion"),
            "duplicate": True,
            "content_hash": content_hash,
            "evidence": existing.get("evidence") or {},
        }


def adjudicate(store, records):
    """Adjudicate records in deterministic event-time order.

    Sorting by ``(timestamp, location hash)`` makes batches replayable and
    independent of page/collection order.  Late observations are still
    detected across batches because the persisted state tracks the newest
    event time ever seen for the POI.
    """
    ordered = sorted(records, key=lambda r: (_order_key(r.get("ts")), location_hash(r)))
    adjudicator = Adjudicator(store)
    return [adjudicator.apply(record) for record in ordered]


def summarize(decisions):
    """Compact summary of a batch of decisions."""
    counts = {}
    for decision in decisions:
        conclusion = decision["conclusion"]
        counts[conclusion] = counts.get(conclusion, 0) + 1
    return {
        "observations": len(decisions),
        "applied": sum(1 for d in decisions
                       if d.get("applied") and not d.get("duplicate")),
        "duplicates": sum(1 for d in decisions if d.get("duplicate")),
        "pois": len({d.get("poi") for d in decisions}),
        "conclusions": counts,
    }

def report(store, poi=None):
    """Full inspection view: raw coordinates, final conclusion and evidence."""
    states = store.all_location_states()
    if poi is not None:
        states = [s for s in states if s.get("poi") == poi]
    pois = []
    for state in states:
        key = state["poi"]
        observations = store.location_observations(key)
        events = store.location_events(key)
        pois.append({
            "poi": key,
            "name": state.get("name"),
            "raw": {"lat": state.get("raw_lat"), "lng": state.get("raw_lng")},
            "latest_raw": {"lat": state.get("latest_raw_lat"),
                           "lng": state.get("latest_raw_lng")},
            "final": {
                "lat": state.get("lat"),
                "lng": state.get("lng"),
                "grid": state.get("grid"),
                "status": state.get("status"),
                "open": state.get("open"),
                "ts": state.get("ts"),
                "last_seen_ts": state.get("last_seen_ts"),
            },
            "conclusion": state.get("final_conclusion") or state.get("conclusion"),
            "evidence": state.get("final_evidence") or state.get("evidence"),
            "latest_conclusion": state.get("conclusion"),
            "latest_evidence": state.get("evidence"),
            "observation_count": len(observations),
            "observations": observations,
            "events": events,
        })
    return {"pois": pois, "count": len(pois)}
