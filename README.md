# Air-Gapped Image Porter

A native desktop tool to **pull container images for a specific CPU architecture**,
**save them to `.tar` files** for transport across an air gap, then **load, retag,
and push** them into a destination registry such as Azure Container Registry (ACR).

Standard-library-only Python (tkinter) — no `pip install` for the app itself.
Runs the same on **Windows, macOS, and Linux**. The only external requirement is a
working **Docker CLI** on your PATH.

## The problem it solves

On an Apple-silicon Mac a plain `docker pull nginx` resolves to the **arm64**
image. Your air-gapped cluster almost certainly runs on **amd64** nodes, so those
images won't start. This tool makes the target platform **explicit** on every pull
(`--platform linux/amd64` by default) and bundles the results for transport.

## Workflow

1. **Login** (top bar) — `docker login` against the source registry. The password /
   access token is fed via `--password-stdin`, so it never appears in argv or the
   process table. Leave *Registry* blank for Docker Hub. Saved logins can be
   recalled from the dropdown; **✎ Rename** renames the selected one in place
   (type the new name in the *Name* box first) rather than saving a duplicate.
2. **Pull & Save tab** (internet-connected machine):
   - Choose the **target platform** (`linux/amd64` default).
   - Paste your image list (one per line; `#` comments and blank lines ignored),
     or load it from a file.
   - Pick an output folder and whether to produce **one bundle tar** (all images in
     a single `docker save`) or **one tar per image**.
   - **Pull & Save** — pulls each image for the chosen platform, then saves.
3. Carry the `.tar`(s) across the air gap.
4. **Load & Push to ACR tab** (air-gapped side):
   - Select the `.tar`(s) and **Load**.
   - Set the destination registry, choose whether to keep the repo namespace, and
     list the images. **Preview retags** shows exactly what each image becomes.
   - **Retag & Push** runs `docker tag` + `docker push` for each.

All output streams live into the bottom **Output** pane; one job runs at a time.
The content area and the Output pane are split by a **draggable sash** — drag it
up to give the log more room (it can't be dragged shut), down to reclaim space for
the current screen. Each screen also **scrolls vertically** when its controls
outgrow the window, so nothing is ever cut off on a small window.

## Prerequisites

1. **Python 3** with **tkinter** — verify: `python -m tkinter` (a small window pops up).
2. **Docker CLI** on your PATH — verify: `docker version`.
   Docker Desktop, Rancher Desktop, colima, or a plain `dockerd` all work.
   On a Mac you can pull foreign architectures thanks to Docker's built-in QEMU
   emulation — you don't need to *run* the amd64 image, only pull & save it.
3. For ACR: be logged in to the destination (`az acr login -n <name>` or
   `docker login <name>.azurecr.io`) on the air-gapped side before pushing.

## Run

```bash
python airgap_porter.py
```

## Navigation

A left **sidebar** switches between screens, grouped:

- **Docker Images** — *Pull & Save*, *Local Images*, *Load & Push*.
- **Helm** — *Helm Charts*.
- **Transport** — *Files* (directory browser), *Preview / Run*.

### Local Images

Lists local images; multi-select to **mass re-prefix** (swap/prepend a registry
prefix — e.g. everything from `oldacr.azurecr.io/...` → `newacr.azurecr.io/...`),
then tag, tag & push, push, or remove. **Preview** shows every `source → dest`.

**Saved targets** — the re-prefix trio (registry prefix, add-path prefix, and the
*keep old registry* flag) can be saved under a name so porting a batch to a new
registry/directory is one click next time. Pick a **Saved target** from the
dropdown to fill the fields; type a **Name** and press **💾 Save** to store the
current fields; **Delete** (two-step confirm) removes one. Targets live in
`~/.airgap-porter/prefix-profiles.json` (no secrets — just registry hostnames and
paths), alongside the saved login and environment profiles.

### Helm Charts

- **Repositories** — add a repo (name + URL), **Update repos** (`helm repo update`).
  **Click a repo** to list every chart it offers, with its chart version and app
  version, in the table below (runs `helm search repo <name>/ --versions`) — no
  need to type a keyword first.
- **Search** — `helm search repo <kw> --versions`; lists every chart version with
  its app version and description.
- **Pull chart(s)** — downloads the selected versions into the charts folder (set
  it with the field + **Browse…**). By default each is saved as a `.tgz` (already
  a gzipped tar, ready for transport); tick **Untar into a folder instead of
  keeping the .tgz** to have helm extract each chart into a `<name>/` directory
  tree instead (`helm pull --untar --untardir`).
- **List images in selected** — renders the chart (`helm template`) and extracts
  the container images it references, then adds them to the **Pull & Save** list
  so you can pull those for your target platform too.
- **Push chart to OCI (ACR)** — `helm registry login` (reuses the top login-bar
  creds) + `helm push <chart.tgz> oci://<acr>.azurecr.io/<ns>`. The **Use saved
  prefix** picker reuses the registry prefixes saved on *Local Images*: pick one
  and (with the **path** box, default `helm`) it fills the OCI target as
  `oci://<prefix>/<path>`, so you don't retype the registry each time.

### Per-pull folders

On **Pull & Save**, tick *put this pull's tars in a subfolder* so each batch
lands in its own folder (named, or an auto timestamp) under the output folder.

### Preview / Run

Pulls, tags, pushes, loads, and chart push/login **stage** here first as a
numbered command list; nothing executes until you press **▶ Run plan**. (Local,
safe operations like `helm repo update` run immediately.)

## Files

- `airgap_porter.py` — the tkinter GUI (sidebar nav, all screens, shared log).
- `docker_ops.py` — Docker CLI logic: argv builders, image-ref parsing, retag/
  reprefix, `docker images` parsing, and the streaming/capture runners.
- `helm_ops.py` — Helm CLI logic: repo/search/pull/template/push argv builders,
  JSON parsers, chart `.tgz` naming, and image extraction from rendered YAML.
  Pure helpers in both modules are unit-testable without Docker/Helm or a display.
- `login_store.py` / `env_store.py` / `prefix_store.py` — persist named profiles in
  `~/.airgap-porter/` (registry logins, session-env sets, and re-prefix targets
  respectively). Free functions, unit-testable, JSON-backed.

## Notes

- Nothing is destructive; the tool only pulls, saves, loads, tags, and pushes.
- Digest-pinned source refs (`@sha256:…`) are pulled as-is; on retag the digest is
  dropped (you can't push a chosen tag *and* a source digest).
