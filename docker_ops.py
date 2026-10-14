"""
docker_ops — thin, testable wrapper around the `docker` CLI.

Mirrors the design of the K8s dashboard's kubectl_collect: everything shells
out to the `docker` already on your PATH, nothing runs on the UI thread, and
command output is streamed back line-by-line through a callback so the GUI can
show live progress. Pure helpers (argv builders, image-name parsing) are kept
free of any UI or subprocess so they can be unit-tested.

Works the same on Windows, macOS, and Linux — the only requirement is a working
Docker CLI (Docker Desktop, colima, Rancher Desktop, or plain dockerd).
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys

# Platforms we care about for air-gapped targets. On an Apple-silicon Mac a bare
# `docker pull` resolves to linux/arm64, which almost never matches the amd64
# nodes in the destination cluster — so the platform is always explicit here.
PLATFORMS = ("linux/amd64", "linux/arm64", "linux/arm/v7")
DEFAULT_PLATFORM = "linux/amd64"


def docker_path() -> str | None:
    """Absolute path to the `docker` binary on PATH, or None if not installed."""
    return shutil.which("docker")


# --- environment / credentials ----------------------------------------------
# Every subprocess here inherits this process's os.environ (no call passes env=).
# So a token command like `aws ecr get-login-password` fails when AWS creds aren't
# in the environment the app was launched with. Rather than make the user quit,
# export in their shell, and relaunch, we let them paste the same `export …` /
# `set …` / `$env:…` lines and write them into os.environ — every later docker/aws
# call then sees them. Kept as free functions (no UI) so they're unit-testable.
_ENV_ASSIGN_RES = (
    re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$"),      # bash/zsh/sh
    re.compile(r"^\s*set\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$"),         # cmd.exe
    re.compile(r"^\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"),   # PowerShell
    re.compile(r"^\s*setx\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$"),      # Windows setx
    re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$"),               # bare KEY=VALUE
)


def _strip_env_quotes(val: str) -> str:
    """Strip one layer of surrounding quotes and a trailing ';' from a value."""
    val = val.strip()
    if val.endswith(";"):
        val = val[:-1].rstrip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val


def parse_env_assignments(text: str) -> dict:
    """Parse pasted credential lines into a {KEY: VALUE} dict.

    Accepts, one assignment per line, any of:
        export KEY=VALUE   set KEY=VALUE   $env:KEY="VALUE"   setx KEY VALUE   KEY=VALUE
    Blank lines and `#` comments are ignored; surrounding quotes and a trailing
    `;` are stripped. Raises ValueError listing lines that match no form so the
    caller can tell the user exactly what to fix.
    """
    out: dict = {}
    bad: list = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for rx in _ENV_ASSIGN_RES:
            m = rx.match(line)
            if m:
                out[m.group(1)] = _strip_env_quotes(m.group(2))
                break
        else:
            bad.append(raw.rstrip())
    if bad:
        raise ValueError("Couldn't parse these line(s):\n  " + "\n  ".join(bad))
    return out


def server_version_argv() -> list[str]:
    """`docker version` of just the *server* — the real "is the daemon up?" test.

    `docker_path()` only proves the CLI is installed; this command talks to the
    daemon and fails (non-zero, empty output) when Docker isn't running or the
    engine is unreachable. Runs through the same PATH `docker` as every other
    command, so it reflects exactly what the launching terminal can reach.
    """
    return ["docker", "version", "--format", "{{.Server.Version}}"]


def format_command(argv) -> str:
    """A copy-pasteable shell string for an argv list (password args masked).

    A plain string is already a shell command (a pasted login pipeline) — it's
    returned unchanged.
    """
    if isinstance(argv, str):
        return argv
    masked = []
    skip = False
    for i, a in enumerate(argv):
        if skip:
            masked.append("********")
            skip = False
            continue
        if a in ("-p", "--password"):
            masked.append(a)
            skip = True
        else:
            masked.append(a)
    return " ".join(shlex.quote(a) for a in masked)


# --- image-reference parsing ------------------------------------------------
# A docker reference is [registry[:port]/]repository[:tag][@digest]. We only
# need enough parsing to (a) split a user's pasted list into clean refs and
# (b) rewrite the registry prefix when retagging for the destination ACR.
_TAG_RE = re.compile(r"^(?P<name>[^@]+?)(?::(?P<tag>[^/:@]+))?(?:@(?P<digest>sha256:[0-9a-f]+))?$")


def parse_images(text: str) -> list[str]:
    """Split a pasted blob (newline/comma separated) into clean image refs.

    Blank lines and `#` comments are dropped; surrounding whitespace trimmed.
    Order is preserved and duplicates removed.
    """
    out: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\n,]", text or ""):
        ref = chunk.strip()
        if not ref or ref.startswith("#"):
            continue
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def split_ref(ref: str) -> tuple[str, str, str, str]:
    """Split an image ref into (registry, repository, tag, digest).

    registry is "" when the ref has no registry host (an implicit Docker Hub
    image like `nginx:latest`). The heuristic matches docker's own: the part
    before the first `/` is a registry only if it contains a '.' or ':' or is
    exactly "localhost".
    """
    registry = ""
    rest = ref
    if "/" in ref:
        head, tail = ref.split("/", 1)
        if "." in head or ":" in head or head == "localhost":
            registry, rest = head, tail
    m = _TAG_RE.match(rest)
    if not m:
        return registry, rest, "", ""
    return registry, m.group("name"), m.group("tag") or "", m.group("digest") or ""


def reprefix(ref: str, new_prefix: str, keep_old_registry: bool = False) -> str:
    """Swap (or prepend) the registry prefix on an image ref, keeping repo+tag.

    This is the mass-retag primitive: pull from one registry, change where the
    images *say* they came from, then push to the new place.

    reprefix("oldacr.azurecr.io/app/api:v2", "newacr.azurecr.io")
        -> "newacr.azurecr.io/app/api:v2"                 (registry swapped)
    reprefix("oldacr.azurecr.io/app/api:v2", "mirror.io", keep_old_registry=True)
        -> "mirror.io/oldacr.azurecr.io/app/api:v2"        (old path preserved)

    `new_prefix` may itself include a namespace segment (e.g. "newacr.azurecr.io/team").
    """
    new_prefix = (new_prefix or "").strip().rstrip("/")
    registry, repo, tag, _digest = split_ref(ref)
    path = f"{registry}/{repo}" if (keep_old_registry and registry) else repo
    out = f"{new_prefix}/{path}" if new_prefix else path
    if tag and tag != "<none>":
        out += f":{tag}"
    return out


def retag_for(ref: str, dest_registry: str, keep_namespace: bool = True) -> str:
    """Compute the destination ACR tag for a source image ref.

    e.g. retag_for("docker.io/library/nginx:1.25", "myacr.azurecr.io")
         -> "myacr.azurecr.io/library/nginx:1.25"
    With keep_namespace=False the leading repo namespace is dropped
         -> "myacr.azurecr.io/nginx:1.25"
    """
    dest_registry = dest_registry.strip().rstrip("/")
    _, repo, tag, digest = split_ref(ref)
    if not keep_namespace and "/" in repo:
        repo = repo.rsplit("/", 1)[-1]
    out = f"{dest_registry}/{repo}" if dest_registry else repo
    if tag:
        out += f":{tag}"
    # A digest-pinned source can't carry its digest onto a new tag; drop it.
    return out


def safe_filename(ref: str) -> str:
    """A filesystem-safe .tar name for an image ref (slashes/colons -> _)."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", ref)
    return f"{base}.tar"


# --- argv builders (pure) ---------------------------------------------------
def login_argv(registry: str, username: str) -> list[str]:
    """`docker login` argv; the password is fed on stdin (never in argv)."""
    argv = ["docker", "login"]
    if username:
        argv += ["-u", username]
    argv += ["--password-stdin"]
    if registry:
        argv.append(registry)
    return argv


# Clients whose `login` subcommand takes -u/-p and will block on a TTY prompt
# when the password is omitted. (az/gcloud/aws use token flows — excluded.)
_LOGIN_BINS = ("docker", "podman", "nerdctl")


def _login_tokens(command: str) -> list[str] | None:
    """Tokenize a login command, or None if it isn't a password-based login.

    Recognizes `docker|podman|nerdctl login …` and `helm registry login …`.
    Pipelines (the ECR `get-login-password | …` case) are not logins we prompt
    for — they already feed a password — so a '|' disqualifies the command.
    """
    if "|" in command:
        return None
    try:
        toks = shlex.split(command, posix=(sys.platform != "win32"))
    except ValueError:
        return None
    if not toks:
        return None
    bin0 = toks[0].lower()
    is_login = ((bin0 in _LOGIN_BINS and "login" in toks[1:])
                or (bin0 == "helm" and "registry" in toks and "login" in toks))
    return toks if is_login else None


def login_needs_password(command: str) -> bool:
    """True if `command` is a login that would block waiting for a password.

    i.e. a recognized login with no -p/--password/--password-stdin already set.
    """
    toks = _login_tokens(command)
    if toks is None:
        return False
    return not any(t in ("-p", "--password", "--password-stdin") for t in toks)


def split_piped_login(command: str):
    """Split a `PRODUCER | docker login … --password-stdin …` into two argv lists.

    Returns (producer_argv, login_argv) so the two halves can be run separately
    *without a shell* — the token command's stdout is captured cleanly and fed to
    `docker login` on stdin. This sidesteps shell-pipe ambiguity (cmd.exe vs sh
    vs PowerShell) and lets us tell whether the token step or the login step
    failed. Returns None for anything that isn't exactly this single-pipe shape
    (those fall back to running through the shell as before).
    """
    if command.count("|") != 1:
        return None
    left, right = (s.strip() for s in command.split("|", 1))
    if not left:
        return None
    try:
        prod = shlex.split(left, posix=True)
        login = shlex.split(right, posix=True)
    except ValueError:
        return None
    if not prod or not login:
        return None
    b = login[0].lower()
    is_login = ((b in _LOGIN_BINS and "login" in login)
                or (b == "helm" and "registry" in login and "login" in login))
    if not is_login or "--password-stdin" not in login:
        return None
    return prod, login


_VALUE_FLAGS = {"-u", "--username", "-p", "--password", "--name", "-r", "--region"}
_LOGIN_WORDS = {"docker", "podman", "nerdctl", "helm", "aws", "az", "login",
                "registry", "ecr", "acr", "get-login-password"}


def login_target(command) -> str:
    """Best-effort registry/host to display for a login (argv list or string).

    `docker login myreg -u u`            -> "myreg"
    `az acr login --name myacr`          -> "myacr"
    `docker login --password-stdin r.io` -> "r.io"
    Returns "" when nothing recognizable is left.
    """
    try:
        toks = command if isinstance(command, list) else shlex.split(command, posix=True)
    except ValueError:
        return ""
    # `az acr login --name X` names the registry explicitly.
    for i, t in enumerate(toks):
        if t == "--name" and i + 1 < len(toks):
            return toks[i + 1]
    positionals, skip = [], False
    for t in toks:
        if skip:
            skip = False
            continue
        if t in _VALUE_FLAGS:
            skip = True
            continue
        if t.startswith("-"):
            continue
        positionals.append(t)
    tail = [p for p in positionals if p.lower() not in _LOGIN_WORDS]
    return tail[-1] if tail else (positionals[-1] if positionals else "")


def build_login_argv(command: str, username: str = "") -> list[str]:
    """Turn a passwordless login *command string* into an argv list to exec.

    Tokenizing and running as argv (rather than appending to a shell string and
    running via the shell) means the username is passed as its own argument and
    can't be interpreted as shell syntax — no quoting/escaping hazard regardless
    of what characters it contains. `--password-stdin` is added so the password
    is fed on stdin. Only valid for logins without a pipe (the prompt case).
    """
    toks = shlex.split(command, posix=True)
    if username:
        toks += ["-u", username]
    toks += ["--password-stdin"]
    return toks


def login_has_username(command: str) -> bool:
    """True if a recognized login command already carries a -u/--username."""
    toks = _login_tokens(command)
    if toks is None:
        return False
    return any(t in ("-u", "--username") for t in toks)


def pull_argv(ref: str, platform: str) -> list[str]:
    """`docker pull --platform <p> <ref>` argv."""
    argv = ["docker", "pull"]
    if platform:
        argv += ["--platform", platform]
    argv.append(ref)
    return argv


def save_argv(refs: list[str], out_file: str) -> list[str]:
    """`docker save -o <file> <ref...>` argv (one tar holding every ref)."""
    return ["docker", "save", "-o", out_file, *refs]


def load_argv(tar_file: str) -> list[str]:
    """`docker load -i <file>` argv."""
    return ["docker", "load", "-i", tar_file]


def tag_argv(src: str, dst: str) -> list[str]:
    """`docker tag <src> <dst>` argv."""
    return ["docker", "tag", src, dst]


def push_argv(ref: str) -> list[str]:
    """`docker push <ref>` argv."""
    return ["docker", "push", ref]


def rmi_argv(ref: str) -> list[str]:
    """`docker rmi <ref>` argv (remove a local image/tag)."""
    return ["docker", "rmi", ref]


# --- listing local images ---------------------------------------------------
# Tab-separated so repos/tags with no awkward chars parse cleanly.
_IMAGE_FORMAT = ("{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedSince}}")


def images_argv() -> list[str]:
    """`docker images` argv emitting one tab-separated row per image."""
    return ["docker", "images", "--format", _IMAGE_FORMAT]


def parse_images_table(out: str) -> list[dict]:
    """Parse `docker images` (our tab format) into row dicts.

    Each row carries a `ref` — "repo:tag" when both are real, else the image ID
    (so dangling/<none> images can still be tagged by ID). Sorted by repo, tag.
    """
    rows = []
    for line in (out or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        repo, tag, iid, size, created = (p.strip() for p in parts[:5])
        has_name = repo and repo != "<none>" and tag and tag != "<none>"
        rows.append({
            "repository": repo, "tag": tag, "id": iid,
            "size": size, "created": created,
            "ref": f"{repo}:{tag}" if has_name else iid,
            "named": has_name,
        })
    rows.sort(key=lambda r: (r["repository"], r["tag"]))
    return rows


# --- runners ----------------------------------------------------------------
def run_capture(argv) -> tuple[int, str]:
    """Run `argv` to completion and return (exit_code, combined_output).

    Used for read-only queries (listing images) where we want the whole result
    at once rather than streamed. Raises FileNotFoundError if docker is missing.
    """
    proc = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    return proc.returncode, proc.stdout


def run_capture_split(argv) -> tuple[int, str, str]:
    """Run `argv`, returning (exit_code, stdout, stderr) kept separate.

    Used for the token half of a piped login (`aws ecr get-login-password`) so
    the token (stdout) stays clean and any error (stderr) is reported on its own.
    Raises FileNotFoundError if the binary is missing.
    """
    proc = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    return proc.returncode, proc.stdout, proc.stderr


# --- streaming runner -------------------------------------------------------
def run_stream(argv, on_line, *, stdin_text: str | None = None) -> int:
    """Run `argv`, streaming combined stdout/stderr to on_line(str) as it comes.

    Returns the process exit code. `stdin_text` (if given) is written to the
    child's stdin and closed — used to feed `docker login --password-stdin`
    without the password ever appearing in argv or the process table.
    Raises FileNotFoundError if the binary is missing (caller reports it).
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        # Don't pop a console window for each docker call on Windows.
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    proc.stdout.close()
    return proc.wait()


def run_shell_stream(command: str, on_line, *, stdin_text: str | None = None) -> int:
    """Run a full shell `command` string (may contain pipes), streaming output.

    Login flows are often pipelines — `aws ecr get-login-password … | docker
    login --password-stdin …` — which an argv list can't express, so these run
    through the system shell (cmd.exe on Windows, /bin/sh elsewhere).

    `stdin_text` (if given) is written to the child's stdin and closed — used to
    feed a prompted password into `docker login --password-stdin` so it never
    appears in argv, the process table, or the saved login profile.
    """
    proc = subprocess.Popen(
        command, shell=True,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    proc.stdout.close()
    return proc.wait()
