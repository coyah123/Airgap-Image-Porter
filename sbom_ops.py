"""
sbom_ops — pull/generate an SPDX SBOM for an image, keeping Syft OPTIONAL.

Docker alone can no longer *generate* an SBOM (the old `docker sbom` plugin is
gone), so there are three tiers, tried in order of reliability/zero-dependency:

  1. syft    (if on PATH) — reads the just-pulled image from the local docker
             daemon and writes clean SPDX JSON. This is what the manager means
             by "swift"; we auto-detect it instead of requiring it.
  2. trivy   (if on PATH) — same idea, also emits SPDX JSON.
  3. buildx attestation — `docker buildx imagetools inspect` pulls an SBOM that
             was ATTACHED to the image in the registry (Docker Official Images,
             anything built with buildkit `--sbom=true`). Zero extra tools, but
             only works when such an attestation exists.

If none of the above yields anything, the caller logs a warning and continues —
the image pull itself never fails just because an SBOM couldn't be produced.

Pure/stdlib only: argv builders + a JSON extractor. Execution is done by the
app via docker_ops.run_capture*, on a background thread (never the UI thread).
"""
from __future__ import annotations

import json
import re
import shutil


def syft_path() -> str | None:
    return shutil.which("syft")


def trivy_path() -> str | None:
    return shutil.which("trivy")


def available_generators() -> list[str]:
    """Which local SBOM generators are installed (for status display)."""
    names = []
    if syft_path():
        names.append("syft")
    if trivy_path():
        names.append("trivy")
    return names


def sbom_filename(ref: str) -> str:
    """An SPDX filename for an image ref: `nginx:1.25` -> `nginx_1.25.spdx.json`."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", ref)
    return f"{base}.spdx.json"


# --- argv builders (pure) ---------------------------------------------------
def syft_argv(ref: str, out_file: str) -> list[str]:
    """`syft <ref> -o spdx-json=<file>` — reads the local docker image."""
    return ["syft", ref, "-q", "-o", f"spdx-json={out_file}"]


def trivy_argv(ref: str, out_file: str) -> list[str]:
    """`trivy image --format spdx-json --output <file> <ref>`."""
    return ["trivy", "image", "--quiet", "--format", "spdx-json",
            "--output", out_file, ref]


def buildx_version_argv() -> list[str]:
    """Probe for buildx (its absence means skip the attestation tier)."""
    return ["docker", "buildx", "version"]


def buildx_sbom_argv(ref: str) -> list[str]:
    """Fetch any SBOM attestation attached to `ref` in its registry.

    `.SBOM` is buildx's wrapper; for a single image it's `{"SPDX": {...}}`, for
    a multi-platform index it's `{"<platform>": {"SPDX": {...}}, ...}`. We take
    the whole thing and pull the SPDX doc out with `extract_spdx` below.
    """
    return ["docker", "buildx", "imagetools", "inspect", ref,
            "--format", "{{ json .SBOM }}"]


def extract_spdx(raw: str, platform: str = "") -> dict | None:
    """Pull an SPDX document out of `docker buildx ... {{ json .SBOM }}` output.

    Returns the SPDX dict, or None if the output is empty/null or carries no
    SBOM. `platform` (e.g. "linux/amd64") picks the matching entry for a
    multi-platform image; otherwise the first available SPDX doc is used.
    """
    text = (raw or "").strip()
    if not text or text == "null":
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    # Single-image shape: {"SPDX": {...}} (possibly with other keys alongside).
    if isinstance(data.get("SPDX"), dict):
        return data["SPDX"]
    # Multi-platform shape: {"linux/amd64": {"SPDX": {...}}, ...}
    plat_entry = data.get(platform) if platform else None
    if isinstance(plat_entry, dict) and isinstance(plat_entry.get("SPDX"), dict):
        return plat_entry["SPDX"]
    for val in data.values():
        if isinstance(val, dict) and isinstance(val.get("SPDX"), dict):
            return val["SPDX"]
    return None
