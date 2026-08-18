"""Read-only directory listing and file access for the dashboard's file
browser, scoped to a configured allow-list of root directories so the web
API can never be used to read arbitrary paths off the Pi's filesystem.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class FileBrowserError(Exception):
    pass


def get_roots(config: dict[str, Any]) -> list[dict[str, Any]]:
    configured = config.get("web", {}).get("browsable_roots")
    if not configured:
        paths = config["paths"]
        configured = [
            {"name": "External SSD", "path": paths["external_ssd_mount"]},
            {"name": "Processed Media", "path": paths["processed_media"]},
        ]
    return [{"name": r["name"], "path": r["path"], "available": Path(r["path"]).is_dir()} for r in configured]


def _resolve_root(config: dict[str, Any], root_name: str) -> Path:
    for root in get_roots(config):
        if root["name"] == root_name:
            return Path(root["path"]).resolve()
    raise FileBrowserError(f"Unknown browsable root: {root_name}")


def _safe_resolve(root: Path, rel_path: str) -> Path:
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root):
        raise FileBrowserError("Path escapes the configured root")
    return candidate


def list_dir(config: dict[str, Any], root_name: str, rel_path: str = "") -> dict[str, Any]:
    root = _resolve_root(config, root_name)
    target = _safe_resolve(root, rel_path)
    if not target.is_dir():
        raise FileBrowserError(f"Not a directory: {rel_path}")

    entries = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        stat = entry.stat()
        entries.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size": stat.st_size if entry.is_file() else None,
            "modified": stat.st_mtime,
        })
    return {"root": root_name, "path": rel_path.strip("/"), "entries": entries}


def resolve_download_path(config: dict[str, Any], root_name: str, rel_path: str) -> Path:
    root = _resolve_root(config, root_name)
    target = _safe_resolve(root, rel_path)
    if not target.is_file():
        raise FileBrowserError(f"Not a file: {rel_path}")
    return target
