"""
tar_map — a small JSON manifest that records which image refs live in which
.tar file, written alongside the tars in the output folder.

Why it exists: on the connected side you pull & save images into a folder over
time (adding more tars as you go). That folder then crosses the air gap. On the
far side you need to know the ORIGINAL image refs to retag/push correctly — the
tar filename alone doesn't tell you (and a bundle holds several images). This map
travels with the tars so the Load & Push side can prefill the exact refs.

Layout — one map file (`airgap-tars.json`) at the ROOT of the folder you point
the loader at. Keys are tar paths RELATIVE to that root (so a per-pull subfolder
is preserved), values carry the image list + a little provenance:

    {
      "version": 1,
      "tars": {
        "pull-20261016-120000/images-bundle.tar": {
          "images": ["nginx:1.25", "myacr.azurecr.io/app/api:v2"],
          "platform": "linux/amd64",
          "saved": "2026-10-16T12:00:00"
        }
      }
    }

Everything is stdlib (json + tarfile), matching the app's no-pip rule.
"""
from __future__ import annotations

import json
import tarfile
import time
from pathlib import Path

MAP_FILENAME = "airgap-tars.json"
RUNBOOK_FILENAME = "LOAD-ON-HIGHSIDE.txt"


def map_path(root) -> Path:
    return Path(root) / MAP_FILENAME


def load(root) -> dict:
    """Read the map at `root`; return an empty skeleton if none/corrupt."""
    p = map_path(root)
    if not p.exists():
        return {"version": 1, "tars": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tars"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "tars": {}}


def record(root, tar_file, images, platform: str = "") -> Path:
    """Merge one tar's entry into the map at `root` and write it back.

    `tar_file` may be absolute or relative; it's stored relative to `root` when
    possible so the folder stays portable across the air gap.
    """
    root = Path(root)
    tar_file = Path(tar_file)
    try:
        key = str(tar_file.relative_to(root)).replace("\\", "/")
    except ValueError:
        key = tar_file.name
    data = load(root)
    data["tars"][key] = {
        "images": list(dict.fromkeys(images)),   # de-dupe, keep order
        "platform": platform,
        "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    map_path(root).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return map_path(root)


def images_in_tar(tar_file) -> list[str]:
    """Best-effort read of a docker-save tar's manifest to recover RepoTags.

    Used as a fallback for tars that were copied in by hand (no map entry).
    `docker save` writes a top-level manifest.json whose entries carry RepoTags.
    """
    refs: list[str] = []
    try:
        with tarfile.open(tar_file, "r") as tf:
            member = tf.extractfile("manifest.json")
            if member is None:
                return []
            manifest = json.loads(member.read().decode("utf-8"))
        for entry in manifest:
            for tag in entry.get("RepoTags") or []:
                if tag and tag not in refs:
                    refs.append(tag)
    except (OSError, tarfile.TarError, ValueError, KeyError):
        return []
    return refs


def scan_folder(root) -> dict:
    """Survey `root` for .tar files and resolve each to its image refs.

    Returns {"tars": [abs_path, ...], "images": [ref, ...]} where images is the
    union across every tar (map entries first, then a manifest fallback for tars
    the map doesn't know about). Tar paths are absolute and de-duplicated.
    """
    root = Path(root)
    data = load(root)
    mapped = data.get("tars", {})

    found_tars: list[Path] = sorted(
        (p for p in root.rglob("*.tar")), key=lambda p: str(p).lower())

    tars: list[str] = []
    images: list[str] = []

    def add_images(refs):
        for r in refs:
            if r and r not in images:
                images.append(r)

    for tar in found_tars:
        tars.append(str(tar))
        rel = str(tar.relative_to(root)).replace("\\", "/")
        entry = mapped.get(rel) or mapped.get(tar.name)
        if entry and entry.get("images"):
            add_images(entry["images"])
        else:
            add_images(images_in_tar(tar))

    return {"tars": tars, "images": images}
