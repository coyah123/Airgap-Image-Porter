"""
env_store — persist named *session environment* profiles for reuse.

A profile is just the block of assignment lines the user pastes into the
Environment / credentials panel (e.g. `USERNAME="00000"` and
`PASSWORD=$(az account get-access-token --query accessToken -o tsv)`), saved
under a name so it can be recalled and re-applied later.

We store the TEXT, not resolved values — the point is that values like
`$(az …)` stay as commands and are evaluated fresh each time the profile is
applied, so tokens never go stale and no live secret is written to disk. As with
login profiles, keep the file private: a profile could still contain a literal
value if the user pastes one. Kept as free functions so they're unit-testable.
"""
from __future__ import annotations

import json
from pathlib import Path

STORE_PATH = Path.home() / ".airgap-porter" / "env-profiles.json"


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("name") and item.get("text") is not None:
            out.append({"name": str(item["name"]), "text": str(item["text"])})
    return out


def load(path: Path = STORE_PATH) -> list[dict]:
    """All saved profiles as [{name, text}] (most-recently-saved first)."""
    return _read(path)


def _write(profiles: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profiles, indent=2), encoding="utf-8")


def upsert(name: str, text: str, path: Path = STORE_PATH) -> list[dict]:
    """Add or replace the profile named `name`, moving it to the front."""
    name, text = name.strip(), text.strip()
    profiles = [p for p in load(path) if p["name"] != name]
    profiles.insert(0, {"name": name, "text": text})
    _write(profiles, path)
    return profiles


def delete(name: str, path: Path = STORE_PATH) -> list[dict]:
    """Remove the profile named `name` (no error if it isn't there)."""
    profiles = [p for p in load(path) if p["name"] != name]
    _write(profiles, path)
    return profiles


def rename(old: str, new: str, path: Path = STORE_PATH) -> list[dict]:
    """Rename profile `old` to `new`, keeping its text and list position.

    No-op if `old` doesn't exist. Any existing profile already named `new` is
    dropped first (the rename would otherwise create a duplicate name).
    """
    old, new = old.strip(), new.strip()
    if not new or old == new:
        return load(path)
    profiles = [p for p in load(path) if p["name"] != new]
    for p in profiles:
        if p["name"] == old:
            p["name"] = new
    _write(profiles, path)
    return profiles


def text_for(name: str, path: Path = STORE_PATH) -> str:
    """The saved assignment text for `name`, or '' if there's no such profile."""
    for p in load(path):
        if p["name"] == name:
            return p["text"]
    return ""
