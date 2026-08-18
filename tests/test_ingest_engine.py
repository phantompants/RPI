import pytest

from src import ingest_engine, state


@pytest.fixture(autouse=True)
def isolated_state(tmp_path):
    state.configure(tmp_path / "state")
    yield


def make_config(tmp_path):
    return {
        "ingest": {
            "media_extensions": [".jpg"],
            "rename_pattern": "{date}-{time}-{original_name}",
            "gpx_match_tolerance_seconds": 120,
        }
    }


def test_structural_card_id_is_deterministic(tmp_path):
    card_dir = tmp_path / "card"
    card_dir.mkdir()
    (card_dir / "DCIM").mkdir()
    (card_dir / "DCIM" / "IMG_0001.JPG").write_bytes(b"fake-image-data")

    first = ingest_engine.get_card_id(str(card_dir))
    second = ingest_engine.get_card_id(str(card_dir))

    assert first == second
    assert len(first) == 16


def test_structural_card_id_changes_with_content(tmp_path):
    card_a = tmp_path / "card_a"
    card_a.mkdir()
    (card_a / "IMG_0001.JPG").write_bytes(b"aaaa")

    card_b = tmp_path / "card_b"
    card_b.mkdir()
    (card_b / "IMG_0001.JPG").write_bytes(b"bbbbbbbbbb")

    assert ingest_engine.get_card_id(str(card_a)) != ingest_engine.get_card_id(str(card_b))


def test_get_card_id_prefers_udev_uuid():
    class FakeDevice:
        def get(self, key):
            return {"ID_FS_UUID": "AAAA-BBBB"}.get(key)

    assert ingest_engine.get_card_id("/does/not/matter", device=FakeDevice()) == "aaaabbbb"


def test_incremental_mirror_copy_skips_unchanged_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"content-a")
    (source / "sub").mkdir()
    (source / "sub" / "b.jpg").write_bytes(b"content-b")

    dest = tmp_path / "dest"

    first_stats = ingest_engine.incremental_mirror_copy(str(source), str(dest))
    assert first_stats["files_copied"] == 2
    assert first_stats["files_skipped"] == 0
    assert (dest / "a.jpg").read_bytes() == b"content-a"
    assert (dest / "sub" / "b.jpg").read_bytes() == b"content-b"

    second_stats = ingest_engine.incremental_mirror_copy(str(source), str(dest))
    assert second_stats["files_copied"] == 0
    assert second_stats["files_skipped"] == 2

    (source / "a.jpg").write_bytes(b"content-a-modified")
    third_stats = ingest_engine.incremental_mirror_copy(str(source), str(dest))
    assert third_stats["files_copied"] == 1
    assert third_stats["files_skipped"] == 1
    assert (dest / "a.jpg").read_bytes() == b"content-a-modified"


def test_process_media_renames_and_avoids_reprocessing(tmp_path):
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    (mirror_dir / "IMG_0001.jpg").write_bytes(b"no-exif-data-here")

    processed_dir = tmp_path / "processed"
    config = make_config(tmp_path)

    first_stats = ingest_engine.process_media(str(mirror_dir), str(processed_dir), config, card_id="card123")
    assert first_stats["files_written"] == 1
    assert first_stats["files_skipped"] == 0

    processed_files = [p for p in processed_dir.iterdir() if p.name != ingest_engine.MANIFEST_NAME]
    assert len(processed_files) == 1
    assert processed_files[0].name.endswith("-IMG_0001.jpg")
    # No EXIF -> falls back to the source file's modification time.
    import datetime
    mtime = datetime.datetime.fromtimestamp((mirror_dir / "IMG_0001.jpg").stat().st_mtime)
    assert processed_files[0].name.startswith(mtime.strftime("%Y%m%d-%H%M%S"))

    second_stats = ingest_engine.process_media(str(mirror_dir), str(processed_dir), config, card_id="card123")
    assert second_stats["files_written"] == 0
    assert second_stats["files_skipped"] == 1


def test_process_media_ignores_non_media_extensions(tmp_path):
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    (mirror_dir / "notes.txt").write_text("not media")

    processed_dir = tmp_path / "processed"
    config = make_config(tmp_path)

    stats = ingest_engine.process_media(str(mirror_dir), str(processed_dir), config, card_id="card456")
    assert stats["files_written"] == 0
    assert list(processed_dir.glob("*.txt")) == []


def test_handle_sd_card_refuses_when_internal_storage_not_mounted(tmp_path):
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "IMG_0001.jpg").write_bytes(b"data")

    config = make_config(tmp_path)
    # A plain directory is not a mount point, so with the guard on
    # (the default), ingest must refuse rather than write to it.
    config["paths"] = {
        "internal_ssd": str(tmp_path / "not_a_mount"),
        "sd_mirror_root": str(tmp_path / "not_a_mount"),
        "processed_media": str(tmp_path / "not_a_mount" / "Processed_Media"),
    }
    (tmp_path / "not_a_mount").mkdir()

    ingest_engine.handle_sd_card(config, str(sd))

    assert list((tmp_path / "not_a_mount").iterdir()) == []  # nothing written


def test_internal_storage_ready_respects_override(tmp_path):
    config = {"paths": {"internal_ssd": str(tmp_path), "require_internal_mount": False}}
    assert ingest_engine.internal_storage_ready(config) is True

    config["paths"]["require_internal_mount"] = True
    assert ingest_engine.internal_storage_ready(config) is False  # tmp dir isn't a mount

    config["paths"]["internal_ssd"] = "/"  # root is always a mount point
    assert ingest_engine.internal_storage_ready(config) is True
