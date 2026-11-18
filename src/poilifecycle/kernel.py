
def _grid(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = 0.0
    return int(round(f * 1000))
def process(record):
    g = _grid(record.get("value"))
    return {"grid": g, "grid_code": "G%d" % g}
def reduce(records):
    last = {}
    for r in sorted(records, key=lambda x: (str(x.get("name")), str(x.get("ts", "")))):
        nm = str(r.get("name")); g = r["grid"]
        r["drift_steps"] = abs(g - last[nm]) if nm in last else 0
        last[nm] = g
    return records
