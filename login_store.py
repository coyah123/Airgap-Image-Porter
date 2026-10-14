"""
login_store — persist named registry-login commands for reuse.

A login isn't always a plain username/password. For ECR it's
`aws ecr get-login-password … | docker login --password-stdin …`; for ACR it's
`az acr login --name <acr>`. So a "login profile" here is just the *command
string* the user would run in a shell — saved under a name so it can be recalled
and re-run later.

Stored as JSON in the user's home dir (NOT in the project tree) because these
commands can contain secrets. Kept as free functions so they're unit-testable.
"""
from __future__ import annotations

import json
from pathlib import Path

STORE_PATH = Path.home() / ".airgap-porter" / "logins.json"


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("name") and item.get("command"):
            out.append({"name": str(item["name"]), "command": str(item["command"])})
    return out


def load(path: Path = STORE_PATH) -> list[dict]:
    """All saved profiles as [{name, command}] (most-recently-saved first)."""
    return _read(path)


def _write(profiles: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profiles, indent=2), encoding="utf-8")


def upsert(name: str, command: str, path: Path = STORE_PATH) -> list[dict]:
    """Add or replace the profile named `name`, moving it to the front."""
    name, command = name.strip(), command.strip()
    profiles = [p for p in load(path) if p["name"] != name]
    profiles.insert(0, {"name": name, "command": command})
    _write(profiles, path)
    return profiles


def delete(name: str, path: Path = STORE_PATH) -> list[dict]:
    """Remove the profile named `name` (no error if it isn't there)."""
    profiles = [p for p in load(path) if p["name"] != name]
    _write(profiles, path)
    return profiles


def command_for(name: str, path: Path = STORE_PATH) -> str:
    """The saved command for `name`, or '' if there's no such profile."""
    for p in load(path):
        if p["name"] == name:
            return p["command"]
    return ""
