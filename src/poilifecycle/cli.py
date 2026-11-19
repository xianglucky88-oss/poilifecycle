
import json, sys
from . import pipeline
from . import location
from .store import Store


def _report(poi=None):
    con = Store().connect()
    try:
        return location.report(Store(con), poi=poi)
    finally:
        con.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("report", "locations"):
        poi = argv[1] if len(argv) > 1 else None
        print(json.dumps(_report(poi=poi), ensure_ascii=False, indent=2))
        return
    mode = "full"
    if "--incremental" in argv:
        mode = "incremental"
    if "--full" in argv:
        mode = "full"
    print(json.dumps(pipeline.run(mode=mode), ensure_ascii=False))

if __name__ == "__main__":
    main()
