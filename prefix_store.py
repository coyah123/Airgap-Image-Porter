"""
prefix_store — persist named *re-prefix* targets for reuse.

When you mass re-tag local images for a new destination you fill in three
things: the **registry prefix** (e.g. `newacr.azurecr.io`), an optional
**path prefix** to prepend (e.g. a project/namespace folder), and whether to
**keep the old registry** in the path. Saving that trio under a name means the
next time you port a batch to the same place you recall it instead of retyping.

Unlike login/env profiles these hold no secrets — just registry hostnames and
paths — but we keep them in the same `~/.airgap-porter` dir for consistency.
Kept as free functions so they're unit-testable.
"""
from __future__ import annotations

import json
from pathlib import Path

STORE_PATH = Path.home() / ".airgap-porter" / "prefix-profiles.json"


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("name") and item.get("prefix") is not None:
            out.append({
                "name": str(item["name"]),
                "prefix": str(item["prefix"]),
                "add": str(item.get("add", "")),
                "keep": bool(item.get("keep", False)),
            })
    return out


def load(path: Path = STORE_PATH) -> list[dict]:
    """All saved targets as [{name, prefix, add, keep}] (most-recent first)."""
    return _read(path)


def _write(profiles: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profiles, indent=2), encoding="utf-8")


def upsert(name: str, prefix: str, add: str = "", keep: bool = False,
           path: Path = STORE_PATH) -> list[dict]:
    """Add or replace the target named `name`, moving it to the front."""
    name, prefix, add = name.strip(), prefix.strip(), add.strip()
    profiles = [p for p in load(path) if p["name"] != name]
    profiles.insert(0, {"name": name, "prefix": prefix, "add": add, "keep": bool(keep)})
    _write(profiles, path)
    return profiles


def delete(name: str, path: Path = STORE_PATH) -> list[dict]:
    """Remove the target named `name` (no error if it isn't there)."""
    profiles = [p for p in load(path) if p["name"] != name]
    _write(profiles, path)
    return profiles


def target_for(name: str, path: Path = STORE_PATH) -> dict | None:
    """The saved {prefix, add, keep} for `name`, or None if there's no such target."""
    for p in load(path):
        if p["name"] == name:
            return {"prefix": p["prefix"], "add": p["add"], "keep": p["keep"]}
    return None
