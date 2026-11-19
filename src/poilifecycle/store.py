
"""Real sqlite3 store: content-hash primary key, INSERT-OR-IGNORE dedup, watermark.

The legacy ``records``/``meta`` tables keep their original shape.  Location
adjudication lives in three additional tables (``location_observations``,
``location_state`` and ``location_events``) which are created with
``IF NOT EXISTS`` so existing databases migrate in place.
"""
import json, sqlite3
from .config import DB_PATH

class Store:
    def __init__(self, con=None):
        self.con = con
        if con is not None:
            self._init()

    def connect(self, path=None):
        con = sqlite3.connect(DB_PATH if path is None else path)
        self.__init__(con)
        return con

    def _init(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS records("
            "content_hash TEXT PRIMARY KEY, id TEXT, ts TEXT, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self._init_location()
        self.con.commit()

    def _init_location(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS location_observations("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "content_hash TEXT UNIQUE NOT NULL,"
            "poi TEXT NOT NULL,"
            "ts TEXT,"
            "raw_lat TEXT, raw_lng TEXT, lat TEXT, lng TEXT,"
            "status TEXT, conclusion TEXT, reason TEXT,"
            "applied INTEGER NOT NULL DEFAULT 0,"
            "payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_location_obs_poi "
            "ON location_observations(poi, seq)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS location_state("
            "poi TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS location_events("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "content_hash TEXT, poi TEXT, ts TEXT,"
            "conclusion TEXT, reason TEXT, applied INTEGER,"
            "detail TEXT NOT NULL)")
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_location_events_poi "
            "ON location_events(poi, seq)")

    @staticmethod
    def hash(record):
        blob = json.dumps(record, sort_keys=True, ensure_ascii=False)
        import hashlib
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def upsert(self, record):
        cur = self.con.execute(
            "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) VALUES(?,?,?,?)",
            (self.hash(record), str(record.get("id", "")),
             str(record.get("ts", "")), json.dumps(record, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount > 0

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def all_records(self):
        return [json.loads(p) for (_h, p) in
                self.con.execute("SELECT content_hash,payload FROM records")]

    def get(self, content_hash):
        row = self.con.execute(
            "SELECT payload FROM records WHERE content_hash=?", (content_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def watermark(self):
        row = self.con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
        return row[0] if row else None

    def set_watermark(self, value):
        self.con.execute(
            "INSERT INTO meta(k,v) VALUES('watermark',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(value),))
        self.con.commit()

    def reset(self):
        self.con.execute("DELETE FROM records")
        self.con.execute("DELETE FROM meta")
        self.con.execute("DELETE FROM location_observations")
        self.con.execute("DELETE FROM location_state")
        self.con.execute("DELETE FROM location_events")
        self.con.commit()

    # ------------------------------------------------------------------ #
    # Location adjudication persistence.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_observation(row):
        (content_hash, poi, ts, raw_lat, raw_lng, lat, lng, status,
         conclusion, reason, applied, payload) = row
        data = json.loads(payload)
        return {
            "content_hash": content_hash,
            "poi": poi,
            "ts": ts,
            "raw_lat": raw_lat,
            "raw_lng": raw_lng,
            "lat": lat,
            "lng": lng,
            "status": status,
            "conclusion": conclusion,
            "reason": reason,
            "applied": bool(applied),
            "raw_record": data.get("raw"),
            "evidence": data.get("evidence"),
        }

    _OBS_SELECT = ("SELECT content_hash,poi,ts,raw_lat,raw_lng,lat,lng,status,"
                   "conclusion,reason,applied,payload FROM location_observations")

    def location_observation(self, content_hash):
        row = self.con.execute(
            self._OBS_SELECT + " WHERE content_hash=?", (content_hash,)).fetchone()
        return self._row_observation(row) if row else None

    def location_observations(self, poi):
        rows = self.con.execute(
            self._OBS_SELECT + " WHERE poi=? ORDER BY seq", (poi,)).fetchall()
        return [self._row_observation(row) for row in rows]

    def location_state(self, poi):
        row = self.con.execute(
            "SELECT payload FROM location_state WHERE poi=?", (poi,)).fetchone()
        return json.loads(row[0]) if row else None

    def all_location_states(self):
        rows = self.con.execute(
            "SELECT payload FROM location_state ORDER BY poi").fetchall()
        return [json.loads(row[0]) for row in rows]

    def location_events(self, poi=None):
        sql = ("SELECT content_hash,poi,ts,conclusion,reason,applied,detail "
               "FROM location_events")
        args = ()
        if poi is not None:
            sql += " WHERE poi=?"
            args = (poi,)
        sql += " ORDER BY seq"
        out = []
        for (content_hash, poi_, ts, conclusion, reason, applied, detail) in \
                self.con.execute(sql, args):
            row = {"content_hash": content_hash, "poi": poi_, "ts": ts,
                   "conclusion": conclusion, "reason": reason,
                   "applied": bool(applied)}
            row.update(json.loads(detail))
            out.append(row)
        return out

    def apply_location(self, content_hash, poi, record, decision, next_state):
        """Persist an observation, its event and the new state atomically.

        Returns ``False`` when the observation was already recorded (a
        concurrent or replayed duplicate) so callers can fall back to the
        previously stored decision without side effects.
        """
        payload = json.dumps({
            "raw": record,
            "evidence": decision.get("evidence"),
        }, sort_keys=True, ensure_ascii=False)
        observed = decision.get("evidence") or {}
        try:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO location_observations("
                "content_hash,poi,ts,raw_lat,raw_lng,lat,lng,status,"
                "conclusion,reason,applied,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (content_hash, poi, None if record.get("ts") is None else str(record.get("ts")),
                 _raw(record.get("lat")), _raw(record.get("lng")),
                 _raw(observed.get("lat")), _raw(observed.get("lng")),
                 None if record.get("status") is None else str(record.get("status")),
                 decision["conclusion"], decision.get("reason"),
                 1 if decision.get("applied") else 0, payload))
            if cur.rowcount <= 0:
                self.con.rollback()
                return False
            self._insert_event(content_hash, poi, record, decision)
            self.con.execute(
                "INSERT INTO location_state(poi,payload) VALUES(?,?) "
                "ON CONFLICT(poi) DO UPDATE SET payload=excluded.payload",
                (poi, json.dumps(next_state, sort_keys=True, ensure_ascii=False)))
            self.con.commit()
        except Exception:
            # A crash mid-write must not leave an observation that would be
            # mistaken for an already-adjudicated duplicate on recovery.
            self.con.rollback()
            raise
        return True

    def _insert_event(self, content_hash, poi, record, decision):
        self.con.execute(
            "INSERT INTO location_events("
            "content_hash,poi,ts,conclusion,reason,applied,detail) "
            "VALUES(?,?,?,?,?,?,?)",
            (content_hash, poi,
             None if record.get("ts") is None else str(record.get("ts")),
             decision["conclusion"], decision.get("reason"),
             1 if decision.get("applied") else 0,
             json.dumps(decision.get("evidence") or {}, sort_keys=True,
                        ensure_ascii=False)))


def _raw(value):
    """Store coordinate text verbatim so raw values survive inspection."""
    if value is None:
        return None
    return str(value)
