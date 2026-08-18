import datetime

from src import export_engine, ingest_engine, state


GPX_TWO_DAYS_UTC = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="-33.8688" lon="151.2093"><time>2026-07-08T22:00:00Z</time></trkpt>
    <trkpt lat="-33.8700" lon="151.2100"><time>2026-07-09T22:30:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""


def local_naive(iso_utc: str) -> datetime.datetime:
    aware = datetime.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return aware.astimezone().replace(tzinfo=None)


def test_gpx_times_converted_to_local_for_matching(tmp_path):
    (tmp_path / "track.gpx").write_text(GPX_TWO_DAYS_UTC)
    track = ingest_engine.GpxTrack.load_from_dir(tmp_path)

    # A photo whose EXIF (local) time is 30s after the first point's local
    # time must match; comparing against raw UTC would miss by hours in any
    # non-UTC timezone.
    photo_time = local_naive("2026-07-08T22:00:00Z") + datetime.timedelta(seconds=30)
    assert track.match(photo_time, tolerance_seconds=120) == (-33.8688, 151.2093)


def test_gpx_point_dates_returns_start_and_days_covered(tmp_path):
    gpx = tmp_path / "track.gpx"
    gpx.write_text(GPX_TWO_DAYS_UTC)
    first, dates = ingest_engine.gpx_point_dates(gpx)

    assert first == local_naive("2026-07-08T22:00:00Z")
    expected_days = {local_naive("2026-07-08T22:00:00Z").date(), local_naive("2026-07-09T22:30:00Z").date()}
    assert dates == expected_days


def test_gpx_point_dates_handles_unparseable_file(tmp_path):
    bad = tmp_path / "bad.gpx"
    bad.write_text("this is not xml")
    first, dates = ingest_engine.gpx_point_dates(bad)
    assert first is None
    assert dates == set()


def test_process_media_names_gpx_by_recording_start(tmp_path):
    state.configure(tmp_path / "state")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "morning_ride.gpx").write_text(GPX_TWO_DAYS_UTC)

    config = {"ingest": {"media_extensions": [".gpx"],
                          "rename_pattern": "{date}-{time}-{original_name}",
                          "gpx_match_tolerance_seconds": 120}}
    processed = tmp_path / "processed"
    stats = ingest_engine.process_media(str(mirror), str(processed), config, card_id="cardgpx")
    assert stats["files_written"] == 1

    files = [p for p in processed.iterdir() if p.suffix == ".gpx"]
    assert len(files) == 1
    expected_prefix = local_naive("2026-07-08T22:00:00Z").strftime("%Y%m%d-%H%M%S")
    assert files[0].name == f"{expected_prefix}-morning_ride.gpx"


def test_export_copies_multiday_gpx_into_each_day_folder(tmp_path):
    state.configure(tmp_path / "state")
    processed = tmp_path / "processed"
    processed.mkdir()
    (processed / "trip.gpx").write_text(GPX_TWO_DAYS_UTC)

    export_cfg = {
        "filter_by_age": False,
        "standard_structure": "{year}/{year}-{month}/{year}-{month}-{day}/{camera}/{filetype}/{resolution}",
    }
    dest = tmp_path / "ssd"
    dest.mkdir()
    stats = export_engine.export_standard(str(processed), str(dest), export_cfg)

    day1 = local_naive("2026-07-08T22:00:00Z").date()
    day2 = local_naive("2026-07-09T22:30:00Z").date()
    copies = sorted(str(p.relative_to(dest)) for p in dest.rglob("trip.gpx"))
    assert len(copies) == len({day1, day2})
    for day in {day1, day2}:
        expected = f"{day.year:04d}/{day.year:04d}-{day.month:02d}/{day.year:04d}-{day.month:02d}-{day.day:02d}/gps_logs/gpx/track/trip.gpx"
        assert expected in copies
    assert stats["files_written"] == len({day1, day2})
