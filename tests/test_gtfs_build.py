"""
The schedule builder, on a tiny hand-made GTFS feed.
"""

import gzip
import io
import zipfile
from array import array

import gtfs_build


def tiny_feed() -> zipfile.ZipFile:
    files = {
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                     "STN,Big Station,37.70,-122.40,1,\n"
                     "P1,Big Station Platform 1,37.7001,-122.4001,0,STN\n"
                     "S2,Second St,37.71,-122.41,0,\n"
                     "S3,Third St,37.72,-122.42,0,\n",
        "routes.txt": "route_id,route_short_name,route_long_name,route_type,route_color,route_text_color\n"
                      "R1,7,HAIGHT,3,FF0000,FFFFFF\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                        "WK,1,1,1,1,1,0,0,20300101,20301231\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,shape_id\n"
                     "R1,WK,T1,Downtown,\nR1,WK,T2,Downtown,\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                          "T1,08:00:00,08:00:00,P1,1\nT1,08:05:00,08:05:00,S2,2\nT1,08:10:00,08:10:00,S3,3\n"
                          "T2,24:50:00,24:50:00,P1,1\nT2,24:55:00,24:55:00,S2,2\nT2,25:00:00,25:00:00,S3,3\n",
        "feed_info.txt": "feed_publisher_name,feed_start_date,feed_end_date,feed_version\nx,20300101,20301231,v1\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return zipfile.ZipFile(buf)


def test_build_groups_trips_into_patterns_and_keeps_ids():
    index, times = gtfs_build.build_agency("SF", tiny_feed())
    assert len(index["patterns"]) == 1                     # same stops, same order: one pattern
    pat = index["patterns"][0]
    assert pat["trips"] == ["T1", "T2"]
    # A platform takes its station's name, and keeps its own stop_id.
    names = [index["stops"][s][0] for s in pat["stops"]]
    assert names[0] == "Big Station" and index["stops"][pat["stops"][0]][3] == "P1"
    # After-midnight times stay past 24:00, as GTFS writes them.
    assert list(times[pat["off"][1]:pat["off"][1] + 3]) == [89400, 89700, 90000]
    assert index["routes"][0]["long"] == "Haight" and index["routes"][0]["id"] == "R1"


def test_output_is_byte_reproducible(tmp_path):
    # The nightly refresh commits only when output changes; a timestamp in the
    # gzip header would make every night a change.
    index, times = gtfs_build.build_agency("SF", tiny_feed())
    gtfs_build.write_agency(tmp_path, "SF", index, times)
    first = (tmp_path / "SF.json.gz").read_bytes(), (tmp_path / "SF.bin.gz").read_bytes()
    index, times = gtfs_build.build_agency("SF", tiny_feed())
    gtfs_build.write_agency(tmp_path, "SF", index, times)
    assert first == ((tmp_path / "SF.json.gz").read_bytes(), (tmp_path / "SF.bin.gz").read_bytes())


def test_times_are_little_endian_int32(tmp_path):
    index, times = gtfs_build.build_agency("SF", tiny_feed())
    gtfs_build.write_agency(tmp_path, "SF", index, times)
    raw = array("i")
    raw.frombytes(gzip.decompress((tmp_path / "SF.bin.gz").read_bytes()))
    assert raw[0] == 8 * 3600


def test_errors_never_contain_the_token(monkeypatch):
    # The Actions log is public; the request URL carries the 511 token.
    class Failing:
        status_code, reason, ok = 404, "Not Found", False
    monkeypatch.setattr(gtfs_build.requests, "get", lambda *a, **k: Failing())
    try:
        gtfs_build._get(gtfs_build._FEED_URL, "SECRET-TOKEN", operator_id="CR")
    except RuntimeError as exc:
        assert "SECRET-TOKEN" not in str(exc) and "404" in str(exc)
    else:
        raise AssertionError("expected an error")
