"""
Build the transit schedule index from 511 SF Bay GTFS feeds.

Run this to (re)generate `transit_data/`, then commit the result:

    set -a && . ./.env && set +a && ./venv/bin/python gtfs_build.py
    ./venv/bin/python gtfs_build.py --from-dir path/to/zips    # offline
    ./venv/bin/python gtfs_build.py --check                    # verify the output

Normally nobody runs it by hand: .github/workflows/transit-schedules.yml
rebuilds nightly and commits only when 511 has published new schedules.

GTFS is the open format every transit agency publishes: stops, lines, the
shape of each line, and every scheduled trip. Muni alone is about 1.3 million
scheduled stop times, far too many to hold as Python objects on a small
server. So the build does the heavy lifting once:

- Trips that visit the same stops in the same order are grouped into a
  *pattern*. A pattern stores its stop list once; each trip then needs only
  its times.
- All times go into one flat array of 32-bit integers (seconds after the
  service day's midnight; GTFS allows 25:10:00 for after-midnight trips),
  written as raw bytes. About 5 MB before compression.
- Each pattern records, for every stop, the nearest point on the line's
  shape, so the planner can cut out exactly the stretch a rider travels.

Why commit the output instead of building on the server: Render's free tier
restarts after 15 minutes idle and has no persistent disk, so a build at
startup would rerun on nearly every visit. The index changes only when the
agencies publish new schedules — check `feed_end` in the index, or
`/api/health`, which reports when the data expires.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import os
import sys
import zipfile
from array import array
from collections import defaultdict
from pathlib import Path

import requests

OPERATORS = ["SF", "BA", "CT"]   # Muni, BART, Caltrain
OUT_DIR = Path(__file__).parent / "transit_data"
_FEED_URL = "https://api.511.org/transit/datafeeds"

# Names riders actually use, instead of the legal agency names in the feeds.
AGENCY_SHORT = {"SF": "Muni", "BA": "BART", "CT": "Caltrain"}


def _rows(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return []
    with z.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))


def _seconds(hms: str) -> int:
    h, m, s = (int(p) for p in hms.strip().split(":"))
    return h * 3600 + m * 60 + s


def _title(name: str) -> str:
    """Muni's long names are upper case ("TARAVAL"); riders see "Taraval"."""
    return name.title() if name.isupper() else name


def _nearest_vertices(stops_xy, shape_xy):
    """For each stop, the index of the nearest shape vertex — searching forward
    only, so a line that doubles back on itself is cut in travel order."""
    out, start = [], 0
    for sx, sy in stops_xy:
        best, best_i = math.inf, start
        for i in range(start, len(shape_xy)):
            dx, dy = shape_xy[i][0] - sx, shape_xy[i][1] - sy
            d = dx * dx + dy * dy
            if d < best:
                best, best_i = d, i
        out.append(best_i)
        start = best_i
    return out


def load_feed(op: str, z: zipfile.ZipFile, index: dict, times: array) -> None:
    agency = AGENCY_SHORT.get(op, op)
    feed = (_rows(z, "feed_info.txt") or [{}])[0]
    index["feeds"].append({"operator": op, "agency": agency,
                           "start": feed.get("feed_start_date"), "end": feed.get("feed_end_date"),
                           "version": feed.get("feed_version")})

    # Stops: platforms take their station's name ("22nd Street", not
    # "22nd Street Caltrain Station Northbound").
    raw_stops = {s["stop_id"]: s for s in _rows(z, "stops.txt")}
    stop_ix = {}
    for sid, s in raw_stops.items():
        if s.get("location_type") not in ("", "0", None):
            continue                                  # stations and entrances aren't boarded
        parent = raw_stops.get(s.get("parent_station") or "")
        name = (parent or s)["stop_name"]
        stop_ix[sid] = len(index["stops"])
        index["stops"].append([name, round(float(s["stop_lat"]), 6), round(float(s["stop_lon"]), 6)])

    route_ix = {}
    for r in _rows(z, "routes.txt"):
        route_ix[r["route_id"]] = len(index["routes"])
        index["routes"].append({
            "agency": agency, "short": r.get("route_short_name") or "",
            "long": _title(r.get("route_long_name") or ""),
            "color": ("#" + r["route_color"]) if r.get("route_color") else None,
            "text_color": ("#" + r["route_text_color"]) if r.get("route_text_color") else None,
            "type": int(r.get("route_type") or 3),
        })

    service_ix = {}
    def service(sid):
        key = f"{op}:{sid}"
        if key not in service_ix:
            service_ix[key] = len(index["services"])
            index["services"].append({"days": "0000000", "start": "0", "end": "0", "add": [], "remove": []})
        return service_ix[key]
    for c in _rows(z, "calendar.txt"):
        entry = index["services"][service(c["service_id"])]
        entry["days"] = "".join(c[d] for d in ("monday", "tuesday", "wednesday", "thursday",
                                                 "friday", "saturday", "sunday"))
        entry["start"], entry["end"] = c["start_date"], c["end_date"]
    for c in _rows(z, "calendar_dates.txt"):
        entry = index["services"][service(c["service_id"])]
        (entry["add"] if c["exception_type"] == "1" else entry["remove"]).append(c["date"])

    shapes_raw = defaultdict(list)
    for p in _rows(z, "shapes.txt"):
        shapes_raw[p["shape_id"]].append((int(p["shape_pt_sequence"]),
                                          float(p["shape_pt_lon"]), float(p["shape_pt_lat"])))
    shape_ix = {}

    trips = {t["trip_id"]: t for t in _rows(z, "trips.txt")}
    by_trip = defaultdict(list)
    with z.open("stop_times.txt") as fh:
        for st in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")):
            if st["stop_id"] not in stop_ix:
                continue
            by_trip[st["trip_id"]].append((int(st["stop_sequence"]), stop_ix[st["stop_id"]],
                                           _seconds(st["departure_time"] or st["arrival_time"])))

    patterns = {}
    for trip_id, visits in by_trip.items():
        trip = trips.get(trip_id)
        if not trip or trip["route_id"] not in route_ix or len(visits) < 2:
            continue
        visits.sort()
        stop_seq = tuple(v[1] for v in visits)
        key = (trip["route_id"], trip.get("trip_headsign") or "", stop_seq)
        pat = patterns.get(key)
        if pat is None:
            sid = trip.get("shape_id") or ""
            if sid and sid in shapes_raw and sid not in shape_ix:
                pts = [(lon, lat) for _, lon, lat in sorted(shapes_raw[sid])]
                shape_ix[sid] = len(index["shapes"])
                index["shapes"].append([v for lon, lat in pts for v in (round(lon * 1e5), round(lat * 1e5))])
            shape = shape_ix.get(sid)
            if shape is not None:
                flat = index["shapes"][shape]
                shape_xy = list(zip(flat[0::2], flat[1::2]))
                stops_xy = [(round(index["stops"][s][2] * 1e5), round(index["stops"][s][1] * 1e5))
                            for s in stop_seq]
                vertices = _nearest_vertices(stops_xy, shape_xy)
            else:
                vertices = None
            pat = patterns[key] = {
                "route": route_ix[trip["route_id"]], "headsign": key[1],
                "stops": list(stop_seq), "shape": shape, "vertices": vertices,
                "svc": [], "off": [],
            }
        pat["svc"].append(service(trip["service_id"]))
        pat["off"].append(len(times))
        times.extend(v[2] for v in visits)

    index["patterns"].extend(patterns.values())
    print(f"  {op} ({agency}): {len(stop_ix)} stops, {len(route_ix)} lines, "
          f"{len(patterns)} patterns, {sum(len(p['off']) for p in patterns.values())} trips",
          file=sys.stderr)


def _write_gz(path: Path, payload: bytes) -> None:
    """Gzip with no timestamp or filename in the header, so the same schedules
    always produce byte-identical files. The nightly refresh commits only
    when the output changes; a timestamp would make every night a change."""
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
        fh.write(payload)


def fetch(op: str, token: str) -> zipfile.ZipFile:
    resp = requests.get(_FEED_URL, params={"api_key": token, "operator_id": op}, timeout=120)
    resp.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(resp.content))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--operators", default=",".join(OPERATORS))
    parser.add_argument("--from-dir", help="read <dir>/<OP>.zip instead of downloading")
    parser.add_argument("--check", action="store_true",
                        help="verify the built data instead of building it")
    args = parser.parse_args()
    if args.check:
        sys.exit(check(args.operators.split(",")))

    token = os.environ.get("TRAFFIC_511_TOKEN")
    if not args.from_dir and not token:
        sys.exit("TRAFFIC_511_TOKEN is not set (load .env with: set -a && . ./.env && set +a)")

    index = {"feeds": [], "stops": [], "routes": [], "services": [], "shapes": [], "patterns": []}
    times = array("i")
    for op in args.operators.split(","):
        z = zipfile.ZipFile(Path(args.from_dir) / f"{op}.zip") if args.from_dir else fetch(op, token)
        load_feed(op, z, index, times)

    if sys.byteorder != "little":
        times.byteswap()                      # stored little-endian regardless of build machine
    OUT_DIR.mkdir(exist_ok=True)
    _write_gz(OUT_DIR / "index.json.gz", json.dumps(index, separators=(",", ":")).encode("utf-8"))
    _write_gz(OUT_DIR / "times.bin.gz", times.tobytes())
    # A small readable summary, so a refresh commit shows at a glance which
    # agency published what, and until when it runs.
    (OUT_DIR / "feeds.json").write_text(json.dumps(index["feeds"], indent=2) + "\n")
    sizes = {p.name: f"{p.stat().st_size / 1e6:.1f} MB" for p in OUT_DIR.iterdir()}
    print(f"wrote {sizes}; {len(times)} stop times", file=sys.stderr)


def check(operators: list[str], warn_days: int = 14) -> int:
    """Sanity-check transit_data/ before it is committed. Returns an exit code.

    Fails when an agency is missing, a known trip no longer plans, or the
    schedules run out within `warn_days` — the last being the case where 511
    hasn't published a newer feed yet and someone should look.
    """
    import transit
    from datetime import date, timedelta

    problems = []
    data = transit._load()
    if data is None:
        return print("no transit data built", file=sys.stderr) or 1
    loaded = {f["operator"] for f in data["feeds"]}
    problems += [f"{op} missing from the build" for op in operators if op not in loaded]
    until = transit.data_until()
    if until and date.fromisoformat(until) < date.today() + timedelta(days=warn_days):
        problems.append(f"schedules expire {until}, within {warn_days} days, and 511 has nothing newer")
    # Powell Station to the Ferry Building: served all day by BART and Muni Metro.
    sample = transit.plan((37.7844, -122.4079), (37.7956, -122.3934), alternatives=False)
    if not sample["routes"] and "walking" not in (sample.get("error") or ""):
        problems.append(f"sample trip failed: {sample.get('error')}")
    for p in problems:
        print(f"::error::{p}", file=sys.stderr)
    print("check ok" if not problems else f"{len(problems)} problem(s)", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    main()
