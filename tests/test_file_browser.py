import pytest

from src import file_browser


def make_config(tmp_path, root_dir):
    return {
        "paths": {"external_ssd_mount": "/does/not/matter", "processed_media": "/does/not/matter"},
        "web": {"browsable_roots": [{"name": "TestRoot", "path": str(root_dir)}]},
    }


def test_list_dir_returns_entries(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "photo.jpg").write_bytes(b"data")
    (root / "sub").mkdir()

    config = make_config(tmp_path, root)
    result = file_browser.list_dir(config, "TestRoot")

    names = {e["name"] for e in result["entries"]}
    assert names == {"photo.jpg", "sub"}


def test_list_dir_navigates_subdirectory(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "nested.txt").write_text("hi")

    config = make_config(tmp_path, root)
    result = file_browser.list_dir(config, "TestRoot", "sub")

    assert [e["name"] for e in result["entries"]] == ["nested.txt"]


def test_list_dir_rejects_path_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "private.txt").write_text("do not read")

    config = make_config(tmp_path, root)
    with pytest.raises(file_browser.FileBrowserError):
        file_browser.list_dir(config, "TestRoot", "../secret")


def test_resolve_download_path_rejects_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("nope")

    config = make_config(tmp_path, root)
    with pytest.raises(file_browser.FileBrowserError):
        file_browser.resolve_download_path(config, "TestRoot", "../outside.txt")


def test_resolve_download_path_returns_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "photo.jpg").write_bytes(b"data")

    config = make_config(tmp_path, root)
    result = file_browser.resolve_download_path(config, "TestRoot", "photo.jpg")
    assert result == (root / "photo.jpg").resolve()


def test_unknown_root_raises():
    config = {"paths": {"external_ssd_mount": "/x", "processed_media": "/y"},
              "web": {"browsable_roots": [{"name": "Known", "path": "/tmp"}]}}
    with pytest.raises(file_browser.FileBrowserError):
        file_browser.list_dir(config, "Unknown")


def test_get_roots_falls_back_to_paths_when_unconfigured(tmp_path):
    config = {"paths": {"external_ssd_mount": str(tmp_path), "processed_media": str(tmp_path)}, "web": {}}
    roots = file_browser.get_roots(config)
    assert {r["name"] for r in roots} == {"External SSD", "Processed Media"}
    assert all(r["available"] for r in roots)
