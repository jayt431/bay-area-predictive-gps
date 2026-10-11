"""
Build the transit schedules from 511 SF Bay's GTFS feeds — every Bay Area agency.

    set -a && . ./.env && set +a && ./venv/bin/python gtfs_build.py   # refresh what changed
    ./venv/bin/python gtfs_build.py --all                              # rebuild every agency
    ./venv/bin/python gtfs_build.py --operators SF,BA                  # just these
    ./venv/bin/python gtfs_build.py --from-dir path/to/zips            # offline, from <OP>.zip
    ./venv/bin/python gtfs_build.py --check                            # verify transit_data/

Normally nobody runs it by hand: .github/workflows/transit-schedules.yml runs
the refresh nightly and commits whatever changed.

GTFS is the open format every transit agency publishes: stops, lines, the
shape of each line, and every scheduled trip. Muni alone is about 1.3 million
scheduled stop times, the whole Bay Area several million — far too many to
hold as Python objects on a small server. So the build does the heavy
lifting once:

- Trips that visit the same stops in the same order are grouped into a
  *pattern*. A pattern stores its stop list once; each trip then needs only
  its times.
- All times go into one flat array of 32-bit integers (seconds after the
  service day's midnight; GTFS allows 25:10:00 for after-midnight trips),
  written as raw bytes.
- Each pattern records, for every stop, the nearest point on the line's
  shape, so the planner can cut out exactly the stretch a rider travels.

**One file pair per agency** (`<OP>.json.gz` + `<OP>.bin.gz`), merged when the
planner loads. A refresh then rebuilds only the agencies that published
something new, and the commit carries only their files — one combined file
would grow the repository by its full size every time any of forty agencies
changed anything.

**What changed is known up front.** 511's operator list reports when each
feed was last generated, so a nightly refresh costs one request for the list
plus one per changed agency — usually none — instead of forty downloads
against a 60-per-hour token limit the live site's traffic alerts share.

`transit_data/feeds.json` records, per agency, the feed version, the dates
it covers, and 511's generation time.
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

OUT_DIR = Path(__file__).parent / "transit_data"
MANIFEST = "feeds.json"
_FEED_URL = "https://api.511.org/transit/datafeeds"
_LIST_URL = "https://api.511.org/transit/gtfsoperators"

# Not timetabled transit: the merged regional feed (it duplicates everyone
# else) and on-demand microtransit, which has no schedule to plan against.
EXCLUDED = {"RG", "SU"}

# The agencies a broken build must never silently lose. A small shuttle
# failing to download is a warning; one of these is an error.
MAJOR = ["SF", "BA", "CT", "AC", "SM", "SC", "GG"]

# Names riders actually use, instead of the legal names in the feeds.
AGENCY_SHORT = {
    "3D": "Tri Delta", "AC": "AC Transit", "AF": "Angel Island Ferry", "AM": "Capitol Corridor",
    "BA": "BART", "CC": "County Connection", "CE": "ACE", "CM": "Commute.org",
    "CR": "Santa Cruz METRO", "CT": "Caltrain", "DE": "Dumbarton Express",
    "EE": "Emery Express", "EM": "Emery Go-Round", "FS": "FAST", "GF": "Golden Gate Ferry",
    "GG": "Golden Gate Transit", "GP": "Rec & Park", "MA": "Marin Transit",
    "MB": "Mission Bay shuttle", "MC": "MV Community Shuttle", "MV": "MVgo",
    "PE": "Petaluma Transit", "PG": "PresidiGo", "RV": "Delta Breeze", "SA": "SMART",
    "SB": "SF Bay Ferry", "SC": "VTA", "SE": "Solano Express", "SF": "Muni",
    "SI": "SFO AirTrain", "SL": "LINKS", "SM": "SamTrans", "SO": "Sonoma County Transit",
    "SQ": "San Joaquins", "SR": "Santa Rosa CityBus", "SS": "South City Shuttle",
    "ST": "SolTrans", "TF": "Treasure Island Ferry", "UC": "Union City Transit",
    "VC": "Vacaville City Coach", "VN": "VINE", "WC": "WestCAT", "WH": "Wheels",
}


class RateLimited(Exception):
    """511 said 429: stop downloading this run and finish the rest next time."""


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


def build_agency(op: str, z: zipfile.ZipFile) -> tuple[dict, array]:
    """One agency's feed as an index (ids local to this agency) and its times."""
    agency = AGENCY_SHORT.get(op, op)
    feed = (_rows(z, "feed_info.txt") or [{}])[0]
    index = {"stops": [], "routes": [], "services": [], "shapes": [], "patterns": [],
             "feed": {"agency": agency, "start": feed.get("feed_start_date"),
                      "end": feed.get("feed_end_date"), "version": feed.get("feed_version")}}
    times = array("i")

    # Stops: platforms take their station's name ("22nd Street", not
    # "22nd Street Caltrain Station Northbound").
    raw_stops = {s["stop_id"]: s for s in _rows(z, "stops.txt")}
    stop_ix = {}
    for sid, s in raw_stops.items():
        if s.get("location_type") not in ("", "0", None):
            continue                                  # stations and entrances aren't boarded
        try:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        except (TypeError, ValueError):
            continue
        parent = raw_stops.get(s.get("parent_station") or "")
        stop_ix[sid] = len(index["stops"])
        index["stops"].append([(parent or s)["stop_name"], round(lat, 6), round(lon, 6)])

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
        if sid not in service_ix:
            service_ix[sid] = len(index["services"])
            index["services"].append({"days": "0000000", "start": "0", "end": "0", "add": [], "remove": []})
        return service_ix[sid]
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
            when = st.get("departure_time") or st.get("arrival_time")
            if st["stop_id"] not in stop_ix or not when:
                continue                              # untimed stops can't be planned against
            by_trip[st["trip_id"]].append((int(st["stop_sequence"]), stop_ix[st["stop_id"]], _seconds(when)))

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

    index["patterns"] = list(patterns.values())
    print(f"  {op} ({agency}): {len(stop_ix)} stops, {len(route_ix)} lines, "
          f"{len(patterns)} patterns, {sum(len(p['off']) for p in patterns.values())} trips",
          file=sys.stderr)
    return index, times


def _write_gz(path: Path, payload: bytes) -> None:
    """Gzip with no timestamp or filename in the header, so the same schedules
    always produce byte-identical files. The nightly refresh commits only
    when the output changes; a timestamp would make every night a change."""
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
        fh.write(payload)


def write_agency(out: Path, op: str, index: dict, times: array) -> None:
    feed = index.pop("feed")
    if sys.byteorder != "little":
        times.byteswap()                      # stored little-endian regardless of build machine
    _write_gz(out / f"{op}.json.gz", json.dumps(index, separators=(",", ":")).encode("utf-8"))
    _write_gz(out / f"{op}.bin.gz", times.tobytes())
    index["feed"] = feed


def _get(url: str, token: str, **params) -> requests.Response:
    """GET from 511. Errors carry the status and the endpoint, never the URL
    as sent: that includes the token, and this runs in a public repository's
    Actions log."""
    try:
        resp = requests.get(url, params={"api_key": token, **params}, timeout=180)
    except requests.RequestException as exc:
        raise RuntimeError(f"{exc.__class__.__name__} fetching {url}") from None
    if resp.status_code == 429:
        raise RateLimited(url)
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} {resp.reason} from {url}"
                           + (f" for {params['operator_id']}" if "operator_id" in params else ""))
    return resp


def published(token: str) -> dict[str, str]:
    """Every agency 511 publishes, mapped to when its feed was last generated."""
    resp = _get(_LIST_URL, token, format="json")
    return {o["Id"]: o.get("LastGenerated") or "" for o in json.loads(resp.content.decode("utf-8-sig"))
            if o["Id"] not in EXCLUDED}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--all", action="store_true", help="rebuild every agency, changed or not")
    parser.add_argument("--operators", help="comma-separated agency ids to rebuild")
    parser.add_argument("--from-dir", help="read <dir>/<OP>.zip instead of downloading")
    parser.add_argument("--out", default=str(OUT_DIR), help="where to write")
    parser.add_argument("--check", action="store_true", help="verify the built data instead of building")
    args = parser.parse_args()
    if args.check:
        sys.exit(check(Path(args.out)))

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    manifest_path = out / MANIFEST
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if isinstance(manifest, list):                    # the single-file layout's summary
        manifest = {}

    token = os.environ.get("TRAFFIC_511_TOKEN")
    if not token and not args.from_dir:
        sys.exit("TRAFFIC_511_TOKEN is not set (load .env with: set -a && . ./.env && set +a)")
    listed = published(token) if token else {}

    if args.operators:
        wanted = args.operators.split(",")
    elif args.all or not listed:
        wanted = sorted(listed) if listed else sorted(p.stem for p in Path(args.from_dir).glob("*.zip"))
    else:
        wanted = sorted(op for op, generated in listed.items()
                        if manifest.get(op, {}).get("generated") != generated)

    built, failed = [], []
    for op in wanted:
        local = Path(args.from_dir) / f"{op}.zip" if args.from_dir else None
        try:
            if local and local.exists():
                z = zipfile.ZipFile(local)
            elif token:
                z = zipfile.ZipFile(io.BytesIO(_get(_FEED_URL, token, operator_id=op).content))
            else:
                continue
            index, times = build_agency(op, z)
        except RateLimited:
            print(f"::warning::511 rate limit reached at {op}; the rest will refresh next run", file=sys.stderr)
            break
        except Exception as exc:                      # one bad feed must not sink the others
            level = "error" if op in MAJOR else "warning"
            kept = "keeping its previous schedules" if op in manifest else "skipped"
            message = str(exc).replace(token, "***") if token else str(exc)
            print(f"::{level}::{op} could not be built ({message}); {kept}", file=sys.stderr)
            failed.append(op)
            continue
        if not index["patterns"]:
            print(f"::warning::{op} published no usable trips; skipped", file=sys.stderr)
            continue
        write_agency(out, op, index, times)
        manifest[op] = {**index["feed"], "generated": listed.get(op, manifest.get(op, {}).get("generated"))}
        built.append(op)

    # An agency 511 no longer publishes is dropped rather than served stale.
    if listed and not args.operators:
        for op in sorted(set(manifest) - set(listed)):
            for suffix in (".json.gz", ".bin.gz"):
                (out / f"{op}{suffix}").unlink(missing_ok=True)
            del manifest[op]
            print(f"  {op} no longer published; removed", file=sys.stderr)

    manifest_path.write_text(json.dumps(dict(sorted(manifest.items())), indent=2) + "\n")
    summary = ", ".join(f"{manifest[op]['agency']} {manifest[op]['version']}" for op in built) or "nothing new"
    print(f"built {len(built)} agencies: {summary}", file=sys.stderr)
    # The nightly workflow uses this line for its commit message.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"summary={summary}\n")
    if any(op in MAJOR for op in failed):
        sys.exit(1)


def check(out: Path, warn_days: int = 14, fail_days: int = 3) -> int:
    """Sanity-check transit_data/ before it is committed. Returns an exit code.

    Errors (fail the run): a major agency missing, a known trip no longer
    planning, or a major agency within `fail_days` of running out. Running
    out within `warn_days` is only a warning: some agencies publish rolling
    three-week feeds (VTA's ends about twenty days after it is generated) and
    replace them a week or so before the end, so a two-week alarm would go red
    in normal operation. Three days left with nothing newer is a real stall.
    """
    import transit
    from datetime import date, timedelta

    transit._DATA_DIR = out
    problems = []
    data = transit._load()
    if data is None:
        print("::error::no transit data built", file=sys.stderr)
        return 1
    loaded = {f["operator"]: f for f in data["feeds"]}
    problems += [f"{op} ({AGENCY_SHORT[op]}) missing from the build" for op in MAJOR if op not in loaded]
    soon = (date.today() + timedelta(days=warn_days)).strftime("%Y%m%d")
    urgent = (date.today() + timedelta(days=fail_days)).strftime("%Y%m%d")
    for op, feed in sorted(loaded.items()):
        if feed.get("end") and feed["end"] < soon:
            message = f"{feed['agency']} schedules end {feed['end']} and 511 has nothing newer yet"
            if op in MAJOR and feed["end"] < urgent:
                problems.append(message)
            else:
                print(f"::warning::{message}", file=sys.stderr)
    # Powell Station to the Ferry Building: served all day by BART and Muni Metro.
    sample = transit.plan((37.7844, -122.4079), (37.7956, -122.3934), alternatives=False)
    if not sample["routes"] and "walking" not in (sample.get("error") or ""):
        problems.append(f"sample trip failed: {sample.get('error')}")
    for p in problems:
        print(f"::error::{p}", file=sys.stderr)
    print(f"{len(loaded)} agencies; " + ("check ok" if not problems else f"{len(problems)} problem(s)"),
          file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    main()
