"""
helm_ops — thin wrapper around the `helm` CLI, matching docker_ops' design.

Pulls Helm charts for transport across an air gap: manage repos, list the
versions available for a chart, discover which container images a chart version
references (so you can pull those too), pull the chart to a local `.tgz`, and
push it into an OCI registry such as Azure ACR.

All argv builders and parsers here are pure — no subprocess, no UI — so they
can be unit-tested. Running the commands is the GUI's job (via docker_ops'
run_stream / run_capture, which are CLI-agnostic).
"""
from __future__ import annotations

import json
import re
import shutil


def helm_path() -> str | None:
    """Absolute path to the `helm` binary on PATH, or None if not installed."""
    return shutil.which("helm")


# --- repos ------------------------------------------------------------------
def repo_list_argv() -> list[str]:
    """`helm repo list -o json` argv."""
    return ["helm", "repo", "list", "-o", "json"]


def repo_add_argv(name: str, url: str) -> list[str]:
    """`helm repo add <name> <url>` argv (--force-update to refresh an existing)."""
    return ["helm", "repo", "add", name, url, "--force-update"]


def repo_update_argv() -> list[str]:
    """`helm repo update` argv (refresh every repo's index)."""
    return ["helm", "repo", "update"]


def parse_repo_list(out: str) -> list[dict]:
    """Parse `helm repo list -o json` into [{name, url}] (empty on no repos)."""
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return []
    rows = []
    for r in data if isinstance(data, list) else []:
        rows.append({"name": r.get("name", ""), "url": r.get("url", "")})
    return rows


# --- searching versions -----------------------------------------------------
def search_argv(keyword: str, versions: bool = True) -> list[str]:
    """`helm search repo <keyword> [--versions] -o json` argv."""
    argv = ["helm", "search", "repo", keyword]
    if versions:
        argv.append("--versions")
    argv += ["-o", "json"]
    return argv


def parse_search(out: str) -> list[dict]:
    """Parse `helm search repo -o json` into version rows.

    Each row: {chart (repo/name), name, version (chart), app_version, description}.
    """
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return []
    rows = []
    for r in data if isinstance(data, list) else []:
        full = r.get("name", "")           # e.g. "bitnami/redis"
        rows.append({
            "chart": full,
            "name": full.split("/", 1)[-1] if "/" in full else full,
            "version": r.get("version", ""),
            "app_version": r.get("app_version", ""),
            "description": r.get("description", ""),
        })
    return rows


# --- pulling ----------------------------------------------------------------
def pull_argv(chart: str, version: str, dest: str, untar: bool = False) -> list[str]:
    """`helm pull <chart> --version <v> -d <dest>` argv.

    `chart` is either "<repo>/<name>" (classic repo) or an "oci://…" reference.
    By default helm writes a `<name>-<version>.tgz` into `dest` — already a
    gzipped tar, ready to carry across the air gap.

    With `untar=True`, helm instead extracts the chart into `dest` as a
    `<name>/` directory tree (`--untar --untardir <dest>`), for when you want
    the unpacked files rather than the archive.
    """
    argv = ["helm", "pull", chart]
    if version:
        argv += ["--version", version]
    if untar:
        argv.append("--untar")
        if dest:
            argv += ["--untardir", dest]
    elif dest:
        argv += ["-d", dest]
    return argv


def tgz_name(chart: str, version: str) -> str:
    """The .tgz filename helm writes for a chart/version ('redis-19.0.1.tgz')."""
    name = chart.split("/", 1)[-1]
    return f"{name}-{version}.tgz" if version else f"{name}.tgz"


# --- discovering the images a chart references ------------------------------
def template_argv(chart: str, version: str) -> list[str]:
    """`helm template <chart> --version <v>` argv — renders manifests to stdout.

    Rendering with default values is the reliable way to learn which container
    images a chart version actually uses; there's no standard chart metadata
    that lists them.
    """
    argv = ["helm", "template", "release", chart]
    if version:
        argv += ["--version", version]
    return argv


# image: lines in rendered Kubernetes YAML — quoted or bare, any indentation.
_IMAGE_RE = re.compile(r"""^\s*-?\s*image:\s*["']?([^"'\s]+)["']?\s*$""", re.MULTILINE)


def extract_images(rendered_yaml: str) -> list[str]:
    """Pull the unique container image refs out of rendered chart YAML.

    Order-preserving, de-duplicated. Templated values that didn't resolve
    (anything still containing '{{') are skipped.
    """
    out, seen = [], set()
    for m in _IMAGE_RE.finditer(rendered_yaml or ""):
        ref = m.group(1).strip()
        if not ref or "{{" in ref or ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
    return out


# --- pushing to an OCI registry (ACR) ---------------------------------------
def registry_login_argv(registry: str, username: str) -> list[str]:
    """`helm registry login <registry> -u <user> --password-stdin` argv."""
    argv = ["helm", "registry", "login", registry]
    if username:
        argv += ["-u", username]
    return argv + ["--password-stdin"]


def push_argv(tgz_path: str, oci_target: str) -> list[str]:
    """`helm push <chart.tgz> <oci://registry/namespace>` argv.

    e.g. push_argv("redis-19.0.1.tgz", "oci://myacr.azurecr.io/helm")
    helm appends the chart name under that OCI path automatically.
    """
    return ["helm", "push", tgz_path, oci_target]
