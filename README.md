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
   process table. Leave *Registry* blank for Docker Hub.
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

### Helm Charts

- **Repositories** — add a repo (name + URL), **Update repos** (`helm repo update`).
- **Search** — `helm search repo <kw> --versions`; lists every chart version with
  its app version and description.
- **Pull chart(s)** — downloads the selected versions as `.tgz` into the charts
  folder (a `.tgz` is already a gzipped tar, ready for transport).
- **List images in selected** — renders the chart (`helm template`) and extracts
  the container images it references, then adds them to the **Pull & Save** list
  so you can pull those for your target platform too.
- **Push chart to OCI (ACR)** — `helm registry login` (reuses the top login-bar
  creds) + `helm push <chart.tgz> oci://<acr>.azurecr.io/<ns>`.

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

## Notes

- Nothing is destructive; the tool only pulls, saves, loads, tags, and pushes.
- Digest-pinned source refs (`@sha256:…`) are pulled as-is; on retag the digest is
  dropped (you can't push a chosen tag *and* a source digest).
