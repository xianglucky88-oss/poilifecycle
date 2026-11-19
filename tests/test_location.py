"""Behavioural tests for location-change adjudication.

These tests use an isolated on-disk SQLite database so persistence,
duplicate processing and failure recovery are exercised for real.
"""
import json
import math

import pytest

from poilifecycle import location
from poilifecycle.store import Store


# A point exactly on a grid line (grid size 0.001 degrees).
BOUNDARY = ("31.230000", "121.473000")


def record(poi="P1", lat=BOUNDARY[0], lng=BOUNDARY[1],
           ts="2030-01-01T00:00:00Z", status="active", name=None):
    return {"id": poi, "name": name or poi, "lat": lat, "lng": lng,
            "ts": ts, "status": status}


@pytest.fixture()
def store(tmp_path):
    con = Store().connect(str(tmp_path / "poi.db"))
    try:
        yield Store(con), con
    finally:
        con.close()


def run(store, records):
    return location.adjudicate(store, records)


def report_for(store, poi="P1"):
    return location.report(store, poi=poi)["pois"][0]


# --------------------------------------------------------------------------- #
# Boundary coordinates.
# --------------------------------------------------------------------------- #
def test_tiny_error_across_a_grid_line_is_jitter_not_movement(store):
    st, _con = store
    decisions = run(st, [
        record(lat="31.2300000"),
        record(lat="31.2299999", ts="2030-01-02T00:00:00Z"),
        record(lat="31.2300001", ts="2030-01-03T00:00:00Z"),
    ])
    assert [d["conclusion"] for d in decisions] == ["new", "jitter", "unchanged"]
    assert decisions[1]["evidence"]["raw_grid"] != decisions[1]["evidence"]["stable_grid"]
    final = report_for(st)["final"]
    # The anchored grid is stable across every boundary flip.
    assert final["grid"] == decisions[0]["evidence"]["raw_grid"]
    assert final["lat"] == 31.23


def test_boundary_jitter_is_stable_across_repeated_runs(store):
    st, _con = store
    run(st, [record()])
    first = report_for(st)["final"]
    for day in range(2, 8):
        run(st, [record(lat="31.2299999" if day % 2 else "31.2300001",
                        ts="2030-01-%02dT00:00:00Z" % day)])
    after = report_for(st)["final"]
    assert after["lat"] == first["lat"]
    assert after["lng"] == first["lng"]
    assert after["grid"] == first["grid"]
    assert after["ts"] == first["ts"]


def test_real_relocation_beyond_jitter_radius_is_kept(store):
    st, _con = store
    decisions = run(st, [
        record(),
        record(lat="31.234000", ts="2030-01-02T00:00:00Z"),
    ])
    assert decisions[1]["conclusion"] == "moved"
    assert decisions[1]["applied"] is True
    assert decisions[1]["evidence"]["distance_m"] > location.JITTER_RADIUS_M
    final = report_for(st)["final"]
    assert (final["lat"], final["lng"]) == (31.234, 121.473)
    assert final["ts"] == "2030-01-02T00:00:00Z"


def test_closed_and_duplicate_reports_are_not_hidden_by_jitter(store):
    st, _con = store
    run(st, [record()])
    decisions = run(st, [
        record(ts="2030-01-02T00:00:00Z", status="withdrawn",
               lat="31.2299999"),
    ])
    assert decisions[0]["conclusion"] == "closed"
    assert report_for(st)["final"]["open"] is False


# --------------------------------------------------------------------------- #
# Missing coordinates and lifecycle.
# --------------------------------------------------------------------------- #
def test_missing_coordinates_retain_the_established_location(store):
    st, _con = store
    run(st, [record()])
    before = report_for(st)["final"]
    decisions = run(st, [
        record(lat="", lng="", ts="2030-01-02T00:00:00Z"),
        record(lat=None, lng=None, ts="2030-01-03T00:00:00Z"),
        record(lat="not-a-number", lng="121.473", ts="2030-01-04T00:00:00Z"),
        record(lat="999", lng="121.473", ts="2030-01-05T00:00:00Z"),
    ])
    assert [d["conclusion"] for d in decisions] == ["missing"] * 4
    after = report_for(st)["final"]
    assert (after["lat"], after["lng"], after["grid"]) == \
        (before["lat"], before["lng"], before["grid"])


def test_partial_coordinates_are_treated_as_missing(store):
    st, _con = store
    decisions = run(st, [record(lat="31.23", lng="")])
    assert decisions[0]["conclusion"] == "missing"
    assert decisions[0]["evidence"]["has_coordinates"] is False


def test_reopening_same_location_keeps_the_stable_grid(store):
    st, _con = store
    run(st, [
        record(),
        record(ts="2030-01-02T00:00:00Z", status="withdrawn"),
        record(ts="2030-01-03T00:00:00Z", status="active",
               lat="31.2300001"),
    ])
    view = report_for(st)
    assert view["final"]["open"] is True
    assert view["conclusion"] == "reopened"
    assert view["final"]["grid"] == "LC:31230:121473"


# --------------------------------------------------------------------------- #
# Out-of-order observations and date precedence.
# --------------------------------------------------------------------------- #
def test_late_observation_does_not_rewrite_history(store):
    st, _con = store
    run(st, [
        record(ts="2030-01-01T00:00:00Z"),
        record(ts="2030-01-10T00:00:00Z", lat="31.240000"),
    ])
    before = report_for(st)["final"]
    decisions = run(st, [
        record(ts="2030-01-05T00:00:00Z", lat="32.000000"),
    ])
    assert decisions[0]["conclusion"] == "late"
    assert decisions[0]["applied"] is False
    assert report_for(st)["final"] == before
    # The late observation is still available as evidence.
    assert report_for(st)["observations"][-1]["conclusion"] == "late"


def test_late_observation_matching_the_anchor_is_backfilled(store):
    st, _con = store
    run(st, [record(ts="2030-01-10T00:00:00Z")])
    decisions = run(st, [record(ts="2030-01-05T00:00:00Z",
                                lat="31.2300001")])
    assert decisions[0]["conclusion"] == "backfilled"
    assert decisions[0]["applied"] is False


def test_late_closure_does_not_close_the_poi(store):
    st, _con = store
    run(st, [record(ts="2030-01-01T00:00:00Z"),
             record(ts="2030-01-10T00:00:00Z", lat="31.234000")])
    decisions = run(st, [record(ts="2030-01-05T00:00:00Z", status="withdrawn")])
    assert decisions[0]["conclusion"] == "late"
    assert report_for(st)["final"]["open"] is True


def test_same_day_conflicting_locations_keep_the_first(store):
    st, _con = store
    decisions = run(st, [
        record(ts="2030-02-01T00:00:00Z"),
        record(ts="2030-02-01T06:00:00Z", lat="31.500000"),
        record(ts="2030-02-01T12:00:00Z", lat="31.2300001"),
    ])
    assert decisions[1]["conclusion"] == "same_date_conflict"
    assert report_for(st)["final"]["lat"] == 31.23


def test_different_days_allow_a_real_move(store):
    st, _con = store
    decisions = run(st, [
        record(ts="2030-02-01T00:00:00Z"),
        record(ts="2030-02-02T00:00:00Z", lat="31.500000"),
    ])
    assert decisions[1]["conclusion"] == "moved"
    assert report_for(st)["final"]["lat"] == 31.5


def test_undated_observation_cannot_supersede_a_dated_location(store):
    st, _con = store
    run(st, [record(ts="2030-04-01T00:00:00Z")])
    decisions = run(st, [record(ts="", lat="32.000000")])
    assert decisions[0]["conclusion"] == "undated"
    assert report_for(st)["final"]["lat"] == 31.23


def test_undated_observation_is_processed_after_dated_ones_in_a_batch(store):
    st, _con = store
    decisions = run(st, [
        record(ts="", lat="32.000000"),
        record(ts="2030-01-01T00:00:00Z"),
    ])
    assert decisions[0]["conclusion"] == "new"
    assert decisions[1]["conclusion"] == "undated"


# --------------------------------------------------------------------------- #
# Duplicates, replayed pages and failure recovery.
# --------------------------------------------------------------------------- #
def test_exact_duplicate_is_recognised_and_does_not_change_state(store):
    st, _con = store
    first = run(st, [record()])
    second = run(st, [record()])
    assert first[0]["conclusion"] == "new"
    assert second[0]["conclusion"] == "duplicate"
    assert second[0]["duplicate"] is True
    assert report_for(st)["observation_count"] == 1


def test_duplicate_page_replay_is_idempotent(store):
    st, _con = store
    page = [record(poi="A"), record(poi="B", lat="31.240000")]
    run(st, page)
    before = {p["poi"]: p["final"] for p in location.report(st)["pois"]}
    run(st, list(page))
    run(st, list(reversed(page)))
    after = {p["poi"]: p["final"] for p in location.report(st)["pois"]}
    assert before == after


def test_coordinate_text_formatting_is_the_same_observation(store):
    st, _con = store
    run(st, [record(lat="31.230000", lng="121.473000")])
    decisions = run(st, [record(lat="31.23", lng="121.473")])
    assert decisions[0]["conclusion"] == "duplicate"


def test_failed_write_leaves_no_partial_state_and_recovers(store, monkeypatch):
    st, _con = store
    run(st, [record()])
    before_state = st.location_state("P1")
    before_obs = st.location_observations("P1")

    from poilifecycle import store as store_module
    original = store_module.Store._insert_event

    def boom(self, *args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store_module.Store, "_insert_event", boom)
    with pytest.raises(RuntimeError):
        run(st, [record(ts="2030-01-02T00:00:00Z", lat="31.240000")])
    monkeypatch.setattr(store_module.Store, "_insert_event", original)

    # Nothing was committed; the pre-crash state is intact.
    assert st.location_state("P1") == before_state
    assert st.location_observations("P1") == before_obs

    # Replaying the observation after recovery applies it exactly once.
    decisions = run(st, [record(ts="2030-01-02T00:00:00Z", lat="31.240000")])
    assert decisions[0]["conclusion"] == "moved"
    assert report_for(st)["final"]["lat"] == 31.24
    assert len(st.location_observations("P1")) == 2
    assert len(st.location_events("P1")) == 2


def test_observation_inserted_twice_in_one_batch_rolls_back_cleanly(store):
    st, _con = store
    decisions = run(st, [record(), record()])
    assert decisions[0]["conclusion"] == "new"
    assert decisions[1]["conclusion"] == "duplicate"
    assert len(st.location_observations("P1")) == 1


# --------------------------------------------------------------------------- #
# Old-data replay.
# --------------------------------------------------------------------------- #
def test_legacy_payload_without_coordinates_replays_as_missing(store):
    st, _con = store
    legacy = {"id": "OLD-1", "name": "Legacy", "value": 7,
              "status": "active", "ts": "2029-12-31T00:00:00Z"}
    decisions = run(st, [legacy])
    assert decisions[0]["conclusion"] == "missing"
    # The legacy content-hash store is untouched by adjudication.
    assert st.count() == 0


def test_replay_after_upgrade_does_not_reanchor_a_boundary_poi(store):
    st, _con = store
    # "Legacy" run anchored the POI on the lower side of the grid line.
    run(st, [record(lat="31.229996")])
    anchor = report_for(st)["final"]
    # Replaying the old batch plus newer raw values must keep the anchor.
    run(st, [
        record(lat="31.229996"),
        record(lat="31.230004", ts="2030-01-05T00:00:00Z"),
    ])
    assert report_for(st)["final"]["grid"] == anchor["grid"]
    assert report_for(st)["final"]["lat"] == anchor["lat"]


# --------------------------------------------------------------------------- #
# Inspection output.
# --------------------------------------------------------------------------- #
def test_report_exposes_raw_final_and_evidence(store):
    st, _con = store
    run(st, [
        record(lat="31.230000", lng="121.473000"),
        record(lat="31.2299999", lng="121.4730001", ts="2030-01-02T00:00:00Z"),
    ])
    view = report_for(st)
    assert view["raw"] == {"lat": "31.230000", "lng": "121.473000"}
    assert view["latest_raw"] == {"lat": "31.2299999", "lng": "121.4730001"}
    assert view["final"]["lat"] == 31.23
    assert view["latest_conclusion"] == "jitter"
    evidence = view["latest_evidence"]
    assert evidence["has_coordinates"] is True
    assert evidence["raw_grid"] != evidence["stable_grid"]
    assert evidence["jitter_radius_m"] == location.JITTER_RADIUS_M
    assert evidence["boundary_proximity_m"] is not None
    assert evidence["ts_relation"] == "newer"
    assert view["observation_count"] == 2
    assert len(view["events"]) == 2


def test_report_serialises_to_json(store):
    st, _con = store
    run(st, [record()])
    payload = location.report(st)
    json.dumps(payload)  # must not raise
    assert payload["count"] == 1


def test_summarize_counts_conclusions(store):
    st, _con = store
    decisions = run(st, [record(), record()])
    summary = location.summarize(decisions)
    assert summary["observations"] == 2
    assert summary["duplicates"] == 1
    assert summary["conclusions"] == {"new": 1, "duplicate": 1}


# --------------------------------------------------------------------------- #
# Grid helper sanity.
# --------------------------------------------------------------------------- #
def test_distance_is_symmetric_and_zero_for_same_point():
    a, b = (31.23, 121.473), (31.24, 121.483)
    assert location.distance_m(a, a) == pytest.approx(0.0, abs=1e-6)
    assert location.distance_m(a, b) == pytest.approx(location.distance_m(b, a))
    assert location.distance_m(a, b) > 1000


def test_boundary_proximity_is_zero_on_a_grid_line():
    assert location.boundary_distance_m(31.23, 121.473) == pytest.approx(0.0, abs=1e-6)
    assert location.boundary_distance_m(31.2305, 121.4735) > 10


# --------------------------------------------------------------------------- #
# Pipeline integration and legacy database migration.
# --------------------------------------------------------------------------- #
def test_store_migrates_an_existing_legacy_database(tmp_path):
    import sqlite3
    path = str(tmp_path / "legacy.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE records(content_hash TEXT PRIMARY KEY, id TEXT, "
                "ts TEXT, payload TEXT NOT NULL)")
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO records VALUES('h1','old','2029-01-01',"
                "'{\"id\":\"old\",\"ts\":\"2029-01-01\"}')")
    con.execute("INSERT INTO meta VALUES('watermark','2029-01-01')")
    con.commit()
    con.close()

    con = Store().connect(path)
    try:
        st = Store(con)
        assert st.count() == 1
        assert st.watermark() == "2029-01-01"
        # New adjudication tables exist alongside the untouched legacy ones.
        run(st, [record(poi="fresh")])
        assert report_for(st, "fresh")["final"]["grid"]
    finally:
        con.close()


def test_incremental_mode_records_backfilled_observations(monkeypatch, tmp_path):
    """A backfilled ("missing report") observation is still adjudicated even
    when the legacy incremental watermark filter excludes it from ingestion."""
    from poilifecycle import framework

    con = Store().connect(str(tmp_path / "inc.db"))
    st = Store(con)
    fixtures = [
        record(poi="P1", ts="2030-01-01T00:00:00Z"),
        record(poi="P1", ts="2030-01-10T00:00:00Z", lat="31.240000"),
    ]
    monkeypatch.setattr(framework, "fetch_all",
                        lambda http: (list(fixtures), 1))
    try:
        framework.run(mode="full", con=con)
        fixtures.append(record(poi="P1", ts="2030-01-05T00:00:00Z",
                               lat="31.2400001"))
        result = framework.run(mode="incremental", con=con)
        assert result["ingested"] == 0  # legacy filter still excludes it
        view = location.report(st, poi="P1")["pois"][0]
    finally:
        con.close()

    assert view["observations"][-1]["conclusion"] == "backfilled"
    assert view["final"]["lat"] == 31.24


def test_pipeline_result_keeps_legacy_fields_and_adds_summary(monkeypatch, tmp_path):
    from poilifecycle import framework

    con = Store().connect(str(tmp_path / "pipe.db"))
    fixtures = [record(poi="P1", ts="2030-01-01T00:00:00Z")]
    monkeypatch.setattr(framework, "fetch_all", lambda http: (list(fixtures), 1))
    try:
        result = framework.run(mode="full", con=con)
    finally:
        con.close()
    for key in ("engine", "mode", "fetched", "duplicates", "ingested",
                "count", "http_attempts", "slept", "watermark"):
        assert key in result
    assert result["locations"]["conclusions"] == {"new": 1}


def test_boundary_anchor_survives_a_process_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    con = Store().connect(path)
    try:
        run(Store(con), [record(lat="31.2300001", ts="2030-01-01T00:00:00Z")])
        first = location.report(Store(con), poi="P1")["pois"][0]["final"]
    finally:
        con.close()

    # New process: a fresh connection re-reads the persisted anchor.
    con = Store().connect(path)
    try:
        run(Store(con), [record(lat="31.2299999", ts="2030-01-02T00:00:00Z")])
        after = location.report(Store(con), poi="P1")["pois"][0]
    finally:
        con.close()
    assert after["final"]["grid"] == first["grid"]
    assert after["final"]["lat"] == first["lat"]
    assert after["latest_conclusion"] == "jitter"
