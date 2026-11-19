
from types import SimpleNamespace

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "poilifecycle.db"

SPEC = SimpleNamespace(
    slug="poilifecycle",
    fmt="json",
    engine="crawl",
    endpoint="items",
    columns=["id", "name", "value", "status", "ts", "lat", "lng"],
    page_size=4,
    int_fields=["value"],
    ts_field="ts",
    # Location adjudication.  A coordinate that sits on a grid line can flip
    # cells under a tiny error; movements within the jitter radius keep the
    # established grid, anything larger is a relocation.
    grid_size=0.001,
    jitter_radius_m=30.0,
    closed_statuses=("closed", "withdrawn", "inactive", "deleted"),
)
