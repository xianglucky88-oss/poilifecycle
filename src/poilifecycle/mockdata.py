
"""Build deterministic source rows for the local fixture."""
import hashlib
from datetime import datetime, timedelta, timezone
from .config import SPEC

NAMES = ["Alpha Widget", "Beta Gadget", "Gamma Set", "Delta Unit"]
STATUS = ["new", "active", "pending", "sold", "withdrawn"]
SOURCE_START = datetime(2030, 1, 1, tzinfo=timezone.utc)
GRID = 0.001
BASE_LAT, BASE_LNG = 31.2304, 121.4737

def _h(salt, i):
    return int(hashlib.md5(("%s-%s-%d" % (SPEC.slug, salt, i)).encode()).hexdigest()[:6], 16)

def _coords(i):
    """Coordinates that deliberately straddle grid lines.

    Every third row is placed exactly on a grid line so a rounding error of a
    few metres flips the derived cell; the remaining rows are offset into the
    middle of a cell.  This lets the fixture exercise boundary handling.
    """
    lat = BASE_LAT + (i // 3) * GRID
    lng = BASE_LNG + (i // 3) * GRID
    if i % 3 == 1:
        lat = round(lat / GRID) * GRID
        lng = round(lng / GRID) * GRID
    elif i % 3 == 2:
        lat = round(lat / GRID) * GRID + GRID / 2
        lng = round(lng / GRID) * GRID + GRID / 2
    else:
        lat = round(lat / GRID) * GRID + GRID / 2
        lng = round(lng / GRID) * GRID
    return "%.6f" % lat, "%.6f" % lng

def build_rows():
    cols = SPEC.columns
    ps = SPEC.page_size
    n = ps * 2 + 1
    rows = []
    for i in range(1, n + 1):
        d = {}
        lat, lng = _coords(i)
        for c in cols:
            if c == "id":
                d[c] = "%s-%04d" % (SPEC.slug[:3].upper(), i)
            elif c == "name":
                d[c] = NAMES[(i - 1) % len(NAMES)]
            elif c == "value":
                d[c] = _h("v", i)
            elif c == "status":
                d[c] = STATUS[(i - 1) % len(STATUS)]
            elif c == "ts":
                d[c] = (SOURCE_START + timedelta(days=i % 5)).isoformat().replace("+00:00", "Z")
            elif c == "lat":
                d[c] = lat
            elif c == "lng":
                d[c] = lng
            else:
                d[c] = "%s_%d" % (c, _h(c, i) % 97)
        rows.append(d)
    rows.append(dict(rows[0]))
    return rows

def extra_row(k):
    d = {}
    lat, lng = _coords(k)
    for c in SPEC.columns:
        if c == "id":
            d[c] = "%s-X%04d" % (SPEC.slug[:3].upper(), k)
        elif c == "ts":
            d[c] = (SOURCE_START + timedelta(days=30 + k)).isoformat().replace("+00:00", "Z")
        elif c == "name":
            d[c] = NAMES[k % len(NAMES)]
        elif c == "value":
            d[c] = _h("x", k)
        elif c == "status":
            d[c] = "active"
        elif c == "lat":
            d[c] = lat
        elif c == "lng":
            d[c] = lng
        else:
            d[c] = "%s_new_%d" % (c, k)
    return d
