"""
Air-Gapped Image Porter — a native desktop tool to pull container images for a
specific CPU architecture, save them to .tar files for transport across an air
gap, then load/retag/push them into a destination registry (e.g. Azure ACR).

Standard library ONLY (tkinter) — no pip install needed for the app itself.
The one external requirement is a working Docker CLI on your PATH. Runs the same
on Windows, macOS, and Linux.

Run:
    python airgap_porter.py

Why it exists: on an Apple-silicon Mac a plain `docker pull` grabs the arm64
image, which won't run on amd64 nodes behind the air gap. This tool makes the
target platform explicit for every pull, then bundles the images for transport.

How it's organized:
  1. docker_ops          — all docker CLI logic (imported; shelled out, threaded).
  2. JobRunner           — serializes work onto a daemon thread, streams output
                           to the shared log via a queue drained on the UI thread.
  3. App (tk.Tk)         — the login bar, the Pull & Save tab, the Load & Push
                           tab, and the shared log pane at the bottom.

Threading rule (same as the K8s dashboard): docker never runs on the UI thread.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import docker_ops
import helm_ops
import login_store


# ---------------------------------------------------------------------------
# JobRunner — runs a list of named steps on a background thread, pushing every
# output line (and step/▶/✓/✗ markers) onto a queue the UI drains. One job at a
# time; the UI disables its action buttons while `busy`.
# ---------------------------------------------------------------------------
class JobRunner:
    """Serialize docker work onto a daemon thread; stream results via a queue."""

    def __init__(self, out_q: "queue.Queue"):
        self.out_q = out_q
        self.busy = False

    def _emit(self, kind: str, text: str = ""):
        self.out_q.put((kind, text))

    def run_steps(self, steps, on_done=None):
        """Run `steps` — a list of (label, argv, stdin_text) — in order.

        Stops at the first non-zero exit. Posts ('done', summary) at the end.
        `steps` may also contain callables taking the emit-line fn (for pure
        Python steps like computing filenames); they must return 0/non-zero.
        """
        if self.busy:
            self._emit("line", "⚠ a job is already running — wait for it to finish.")
            return
        self.busy = True
        threading.Thread(target=self._worker, args=(steps, on_done), daemon=True).start()

    def _worker(self, steps, on_done):
        failed = None
        try:
            for step in steps:
                label, argv, stdin_text = step
                self._emit("step", label)
                self._emit("cmd", docker_ops.format_command(argv))
                try:
                    if isinstance(argv, str):
                        # A pasted shell command (e.g. a login pipeline).
                        code = docker_ops.run_shell_stream(
                            argv, lambda ln: self._emit("line", ln))
                    else:
                        code = docker_ops.run_stream(
                            argv, lambda ln: self._emit("line", ln),
                            stdin_text=stdin_text)
                except FileNotFoundError:
                    self._emit("line", "✗ command not found on PATH.")
                    failed = label
                    break
                if code != 0:
                    self._emit("line", f"✗ exit code {code}")
                    failed = label
                    break
                self._emit("ok", label)
        finally:
            self.busy = False
            self._emit("done", failed or "")
            if on_done:
                on_done(failed)


class App(tk.Tk):
    """The main window: login bar, Pull & Save tab, Load & Push tab, log pane."""

    def __init__(self):
        super().__init__()
        self.title("📦 Air-Gapped Image Porter")
        self.geometry("1040x780")
        self.geometry("1180x840")
        self._q: queue.Queue = queue.Queue()
        self._img_q: queue.Queue = queue.Queue()
        self._helm_q: queue.Queue = queue.Queue()
        self._status_q: queue.Queue = queue.Queue()
        self.runner = JobRunner(self._q)
        self._action_btns: list[ttk.Button] = []
        self._img_rows: list[dict] = []   # last-loaded `docker images` rows
        self._pending_steps: list = []    # commands staged in the Preview tab
        self._pending_after = None         # callable to run when the plan finishes
        self._after_job = None
        self._helm_versions: list[dict] = []  # last `helm search` result rows

        self._init_style()
        self._build_status_bar()
        self._build_login_bar()

        # Body = left sidebar (nav) + a notebook whose tab strip is hidden; the
        # sidebar buttons drive which screen shows. Same pattern as the K8s app.
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        self.sidebar = ttk.Frame(body, padding=(2, 6), style="Sidebar.TFrame")
        self.sidebar.pack(side="left", fill="y")

        self.nb = ttk.Notebook(body, style="Hidden.TNotebook")
        self.nb.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.nb.bind("<<NotebookTabChanged>>", self._on_screen_changed)

        # (icon, label, builder) — order defines sidebar order; headers are plain.
        self._screens = {}   # label -> tab frame
        self._build_pull_tab()
        self._build_images_tab()
        self._build_push_tab()
        self._build_helm_tab()
        self._build_files_tab()
        self._build_preview_tab()
        self._build_sidebar_nav()

        self._build_log_pane()

        self._refresh_docker_status()
        if not docker_ops.docker_path():
            self._log("⚠ docker not found on PATH. Install Docker Desktop / dockerd "
                      "and reopen, or the Pull/Save/Push actions will fail.")
        else:
            self._log(f"docker found: {docker_ops.docker_path()}")
            self._refresh_images()
        if helm_ops.helm_path():
            self._log(f"helm found: {helm_ops.helm_path()}")
            self._refresh_repos()
        else:
            self._log("⚠ helm not found on PATH — the Helm Charts screen needs it.")

        self.nb.select(self._screens["Pull & Save"])
        self.after(120, self._drain)

    # -- styling + sidebar navigation --------------------------------------
    def _init_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # Hide the notebook's tab strip — navigation lives in the sidebar.
        style.layout("Hidden.TNotebook.Tab", [])
        style.configure("Sidebar.TFrame", background="#eef1f5")
        style.configure("Nav.TButton", anchor="w", padding=(10, 8), width=20,
                        font=("", 10))
        style.configure("NavHeader.TLabel", background="#eef1f5",
                        foreground="#667085", font=("", 8, "bold"))
        style.configure("Accent.TButton", font=("", 10, "bold"))
        style.configure("Status.TFrame", background="#f4f6f9")

    # -- Docker connection status banner (top of window) -------------------
    def _build_status_bar(self):
        """A red/green Docker-daemon indicator with a manual refresh button.

        The check shells out to the same PATH `docker` everything else uses, so
        the dot reflects exactly what the launching terminal can reach — green
        only when the daemon actually answers, red when it's missing or down.
        """
        bar = ttk.Frame(self, padding=(10, 6), style="Status.TFrame")
        bar.pack(fill="x", padx=8, pady=(8, 0))
        # Classic tk.Label so we can recolor the dot directly (ttk theming the
        # foreground of a single glyph is fiddly under 'clam').
        self.docker_dot = tk.Label(bar, text="●", font=("", 15), fg="#999",
                                   bg="#f4f6f9")
        self.docker_dot.pack(side="left")
        self.docker_status_lbl = ttk.Label(bar, text="Checking Docker…",
                                            font=("", 10, "bold"))
        self.docker_status_lbl.pack(side="left", padx=(6, 0))
        self.docker_detail_lbl = ttk.Label(bar, text="", foreground="#777")
        self.docker_detail_lbl.pack(side="left", padx=(12, 0))
        self.docker_refresh_btn = ttk.Button(bar, text="⟳ Refresh",
                                              command=self._refresh_docker_status)
        self.docker_refresh_btn.pack(side="right")

    def _refresh_docker_status(self):
        """Re-check the daemon on a background thread (amber dot meanwhile)."""
        self.docker_dot.config(fg="#e0a800")
        self.docker_status_lbl.config(text="Checking Docker…")
        self.docker_detail_lbl.config(text="")
        threading.Thread(target=self._docker_status_worker, daemon=True).start()

    def _docker_status_worker(self):
        path = docker_ops.docker_path()
        if not path:
            self._status_q.put(("down", "docker not found on PATH"))
            return
        try:
            code, out = docker_ops.run_capture(docker_ops.server_version_argv())
        except FileNotFoundError:
            self._status_q.put(("down", "docker not found on PATH"))
            return
        if code == 0 and out.strip():
            self._status_q.put(("up", out.strip()))
        else:
            # A running CLI but unreachable daemon prints its error to stderr.
            self._status_q.put(("down", out.strip().splitlines()[-1] if out.strip()
                                else "daemon not responding"))

    def _build_sidebar_nav(self):
        """One button per screen, grouped under section headers."""
        self._nav_buttons = {}
        ttk.Label(self.sidebar, text="📦 Image Porter", style="NavHeader.TLabel",
                  font=("", 11, "bold")).pack(anchor="w", padx=8, pady=(2, 10))
        layout = [
            ("DOCKER IMAGES", ["Pull & Save", "Local Images", "Load & Push"]),
            ("HELM", ["Helm Charts"]),
            ("TRANSPORT", ["Files", "Preview / Run"]),
        ]
        for header, labels in layout:
            ttk.Label(self.sidebar, text=header, style="NavHeader.TLabel").pack(
                anchor="w", padx=10, pady=(10, 2))
            for label in labels:
                b = ttk.Button(self.sidebar, text=label, style="Nav.TButton",
                               command=lambda fr=self._screens[label]: self.nb.select(fr))
                b.pack(fill="x", padx=4, pady=1)
                self._nav_buttons[label] = b

    def _on_screen_changed(self, _e=None):
        """Highlight the active sidebar button; refresh file browser on entry."""
        sel = self.nb.select()
        for label, b in getattr(self, "_nav_buttons", {}).items():
            b.state(["pressed"] if str(self._screens[label]) == str(sel) else ["!pressed"])
        if sel and str(sel) == str(self._screens.get("Files")):
            self._refresh_files()

    # -- login bar (paste any login command; save named profiles) -----------
    _LOGIN_PLACEHOLDER = (
        "# Paste a full login command (pipelines OK), e.g.:\n"
        "# docker login myregistry.io -u user -p <token>\n"
        "# aws ecr get-login-password --region us-east-1 | docker login "
        "--username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com\n"
        "# az acr login --name myacr")

    def _build_login_bar(self):
        f = ttk.LabelFrame(self, text="Registry login", padding=8)
        f.pack(fill="x", padx=8, pady=8)

        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Saved login:").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(row, textvariable=self.profile_var, width=24,
                                        state="readonly")
        self.profile_box.pack(side="left", padx=4)
        self.profile_box.bind("<<ComboboxSelected>>", lambda _e: self._load_profile())
        ttk.Button(row, text="Delete", command=self._delete_profile).pack(side="left")
        ttk.Label(row, text="   Name:").pack(side="left")
        self.profile_name_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.profile_name_var, width=18).pack(side="left", padx=4)
        ttk.Button(row, text="💾 Save", command=self._save_profile).pack(side="left")
        run = ttk.Button(row, text="🔑 Run login", style="Accent.TButton",
                         command=self._do_login)
        run.pack(side="right")
        self._action_btns.append(run)

        self.login_cmd = scrolledtext.ScrolledText(f, height=4, wrap="word",
                                                   font=("Consolas", 9))
        self.login_cmd.pack(fill="x", pady=(6, 0))
        self.login_cmd.insert("1.0", self._LOGIN_PLACEHOLDER)
        ttk.Label(f, text="Runs through your shell. Saved profiles live in "
                  f"{login_store.STORE_PATH} (may contain secrets — keep it private).",
                  foreground="#777").pack(anchor="w", pady=(4, 0))
        self._reload_profiles()

    def _login_command_text(self) -> str:
        txt = self.login_cmd.get("1.0", "end")
        # Drop comment lines so a pasted example with leading #-notes still runs.
        lines = [ln for ln in txt.splitlines() if ln.strip()
                 and not ln.lstrip().startswith("#")]
        return "\n".join(lines).strip()

    def _reload_profiles(self, select: str = ""):
        self._profiles = login_store.load()
        names = [p["name"] for p in self._profiles]
        self.profile_box["values"] = names
        if select and select in names:
            self.profile_var.set(select)

    def _load_profile(self):
        cmd = login_store.command_for(self.profile_var.get())
        if cmd:
            self.login_cmd.delete("1.0", "end")
            self.login_cmd.insert("1.0", cmd)
            self.profile_name_var.set(self.profile_var.get())

    def _save_profile(self):
        name = self.profile_name_var.get().strip() or self.profile_var.get().strip()
        cmd = self._login_command_text()
        if not name:
            messagebox.showwarning("Save login", "Give the login a name first.")
            return
        if not cmd:
            messagebox.showwarning("Save login", "Nothing to save — paste a command.")
            return
        login_store.upsert(name, cmd)
        self._reload_profiles(select=name)
        self._log(f"✓ saved login profile “{name}”.")

    def _delete_profile(self):
        name = self.profile_var.get().strip()
        if not name:
            return
        if messagebox.askyesno("Delete login", f"Delete saved login “{name}”?"):
            login_store.delete(name)
            self.profile_var.set("")
            self._reload_profiles()

    def _do_login(self):
        cmd = self._login_command_text()
        if not cmd:
            messagebox.showwarning("Login", "Paste a login command first.")
            return
        # Login fetches/sets creds locally — run it directly (not via the plan).
        self._run_now([("registry login", cmd, None)])

    # -- Pull & Save tab ----------------------------------------------------
    def _build_pull_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Pull & Save")
        self._screens["Pull & Save"] = tab

        # Platform picker — the whole reason this tool exists.
        plat = ttk.Frame(tab)
        plat.pack(fill="x")
        ttk.Label(plat, text="Target platform:", font=("", 10, "bold")).pack(side="left")
        self.platform_var = tk.StringVar(value=docker_ops.DEFAULT_PLATFORM)
        for p in docker_ops.PLATFORMS:
            ttk.Radiobutton(plat, text=p, value=p,
                            variable=self.platform_var).pack(side="left", padx=6)

        ttk.Label(tab, text="Images (one per line; # comments and blank lines ignored):"
                  ).pack(anchor="w", pady=(10, 2))
        self.images_txt = scrolledtext.ScrolledText(tab, height=10, wrap="none")
        self.images_txt.pack(fill="both", expand=True)
        self.images_txt.insert("1.0", "# e.g.\n# nginx:1.25\n# myacr.azurecr.io/app/api:v2\n")

        filebar = ttk.Frame(tab)
        filebar.pack(fill="x", pady=4)
        ttk.Button(filebar, text="Load list from file…",
                   command=self._load_image_list).pack(side="left")

        out = ttk.Frame(tab)
        out.pack(fill="x", pady=(8, 2))
        ttk.Label(out, text="Output folder:").pack(side="left")
        self.outdir_var = tk.StringVar(value=str(Path.cwd() / "image-tars"))
        ttk.Entry(out, textvariable=self.outdir_var).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(out, text="Browse…", command=self._browse_outdir).pack(side="left")

        # Per-pull subfolder: every tar from this pull lands in its own folder so
        # a batch stays together and doesn't mix with earlier pulls.
        sub = ttk.Frame(tab)
        sub.pack(fill="x", pady=2)
        self.subfolder_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(sub, text="put this pull's tars in a subfolder named:",
                        variable=self.subfolder_on).pack(side="left")
        self.subfolder_var = tk.StringVar(value="")
        ttk.Entry(sub, textvariable=self.subfolder_var, width=28).pack(side="left", padx=4)
        ttk.Label(sub, text="(blank = timestamp)", foreground="#777").pack(side="left")

        mode = ttk.Frame(tab)
        mode.pack(fill="x", pady=2)
        self.bundle_var = tk.StringVar(value="single")
        ttk.Label(mode, text="Save as:").pack(side="left")
        ttk.Radiobutton(mode, text="one bundle tar (all images)", value="single",
                        variable=self.bundle_var).pack(side="left", padx=6)
        ttk.Radiobutton(mode, text="one tar per image", value="per-image",
                        variable=self.bundle_var).pack(side="left", padx=6)
        ttk.Label(mode, text="Bundle name:").pack(side="left", padx=(12, 2))
        self.bundle_name_var = tk.StringVar(value="images-bundle.tar")
        ttk.Entry(mode, textvariable=self.bundle_name_var, width=22).pack(side="left")

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=(10, 0))
        for text, cmd in (("⬇ Pull only", lambda: self._pull_and_save(save=False)),
                          ("💾 Save only", lambda: self._pull_and_save(pull=False)),
                          ("⬇💾 Pull & Save", lambda: self._pull_and_save())):
            b = ttk.Button(act, text=text, command=cmd)
            b.pack(side="left", padx=4)
            self._action_btns.append(b)

    def _load_image_list(self):
        path = filedialog.askopenfilename(
            title="Load image list", filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if path:
            try:
                self.images_txt.delete("1.0", "end")
                self.images_txt.insert("1.0", Path(path).read_text(encoding="utf-8"))
            except OSError as e:
                messagebox.showerror("Load list", str(e))

    def _browse_outdir(self):
        d = filedialog.askdirectory(title="Output folder for .tar files")
        if d:
            self.outdir_var.set(d)

    def _current_images(self) -> list[str]:
        return docker_ops.parse_images(self.images_txt.get("1.0", "end"))

    def _append_pull_images(self, refs):
        """Add image refs to the Pull & Save list (skipping ones already there)."""
        have = set(self._current_images())
        new = [r for r in refs if r not in have]
        if not new:
            return
        if self.images_txt.get("1.0", "end").strip():
            self.images_txt.insert("end", "\n")
        self.images_txt.insert("end", "\n".join(new) + "\n")

    def _pull_and_save(self, pull: bool = True, save: bool = True):
        images = self._current_images()
        if not images:
            messagebox.showwarning("No images", "Add at least one image reference.")
            return
        outdir = Path(self.outdir_var.get().strip() or ".")
        if save and self.subfolder_on.get():
            name = self.subfolder_var.get().strip() or time.strftime("pull-%Y%m%d-%H%M%S")
            outdir = outdir / name
        platform = self.platform_var.get()
        steps = []
        if pull:
            for ref in images:
                steps.append((f"pull {ref} ({platform})",
                              docker_ops.pull_argv(ref, platform), None))
        if save:
            try:
                outdir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Output folder", str(e))
                return
            if self.bundle_var.get() == "single":
                name = self.bundle_name_var.get().strip() or "images-bundle.tar"
                if not name.endswith(".tar"):
                    name += ".tar"
                target = str(outdir / name)
                steps.append((f"save {len(images)} image(s) → {name}",
                              docker_ops.save_argv(images, target), None))
            else:
                for ref in images:
                    target = str(outdir / docker_ops.safe_filename(ref))
                    steps.append((f"save {ref} → {Path(target).name}",
                                  docker_ops.save_argv([ref], target), None))
        self._start(steps, after=(self._refresh_files if save else None))

    # -- Images tab (view, mass re-prefix/tag, push, remove) ----------------
    def _build_images_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Local Images")
        self._screens["Local Images"] = tab

        top = ttk.Frame(tab)
        top.pack(fill="x")
        b = ttk.Button(top, text="⟳ Refresh", command=self._refresh_images)
        b.pack(side="left")
        self._action_btns.append(b)
        ttk.Label(top, text="Filter:").pack(side="left", padx=(12, 2))
        self.img_filter_var = tk.StringVar()
        self.img_filter_var.trace_add("write", lambda *_: self._populate_images())
        ttk.Entry(top, textvariable=self.img_filter_var, width=28).pack(side="left")
        self.img_count = ttk.Label(top, text="", foreground="#777")
        self.img_count.pack(side="right")

        cols = ("repository", "tag", "id", "size", "created")
        tree = ttk.Treeview(tab, columns=cols, show="headings",
                            selectmode="extended", height=12)
        widths = {"repository": 360, "tag": 120, "id": 120, "size": 90, "created": 140}
        for c in cols:
            tree.heading(c, text=c.capitalize())
            tree.column(c, width=widths[c], anchor="w")
        tree.pack(fill="both", expand=True, pady=(6, 4))
        vsb = ttk.Scrollbar(tab, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        self.img_tree = tree

        # --- mass re-prefix controls ---
        box = ttk.LabelFrame(tab, text="Mass re-tag selected images with a new prefix",
                            padding=8)
        box.pack(fill="x", pady=(6, 0))
        row1 = ttk.Frame(box)
        row1.pack(fill="x")
        ttk.Label(row1, text="New prefix:").pack(side="left")
        self.prefix_var = tk.StringVar(value="myacr.azurecr.io")
        ttk.Entry(row1, textvariable=self.prefix_var, width=34).pack(side="left", padx=4)
        self.keep_old_reg_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="keep old registry in path",
                        variable=self.keep_old_reg_var).pack(side="left", padx=6)
        ttk.Button(row1, text="Preview", command=self._preview_reprefix).pack(
            side="left", padx=(12, 0))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(8, 0))
        for text, cmd in (("🏷️ Tag selected", lambda: self._reprefix_selected(push=False)),
                          ("🏷️⬆ Tag & Push selected", lambda: self._reprefix_selected(push=True)),
                          ("⬆ Push selected (as-is)", self._push_selected),
                          ("🗑 Remove selected", self._remove_selected)):
            btn = ttk.Button(row2, text=text, command=cmd)
            btn.pack(side="left", padx=(0, 6))
            self._action_btns.append(btn)

    def _refresh_images(self):
        self.status.config(text="Loading images…")
        threading.Thread(target=self._images_worker, daemon=True).start()

    def _images_worker(self):
        try:
            code, out = docker_ops.run_capture(docker_ops.images_argv())
        except FileNotFoundError:
            self._img_q.put(("err", "docker not found on PATH."))
            return
        if code != 0:
            self._img_q.put(("err", out.strip() or f"docker images exit {code}"))
            return
        self._img_q.put(("rows", docker_ops.parse_images_table(out)))

    def _populate_images(self):
        """Render self._img_rows into the tree, honoring the filter box."""
        needle = self.img_filter_var.get().strip().lower()
        self.img_tree.delete(*self.img_tree.get_children(""))
        shown = 0
        for r in self._img_rows:
            hay = f'{r["repository"]}:{r["tag"]}'.lower()
            if needle and needle not in hay:
                continue
            # iid value is stashed as the item id so selection maps back to a ref.
            self.img_tree.insert("", "end", iid=f'{r["ref"]}\x00{shown}',
                                 values=(r["repository"], r["tag"], r["id"],
                                         r["size"], r["created"]))
            shown += 1
        self.img_count.config(text=f"{shown} / {len(self._img_rows)} images")

    def _selected_refs(self) -> list[str]:
        """The `ref` (repo:tag or image id) of each selected tree row."""
        return [iid.split("\x00", 1)[0] for iid in self.img_tree.selection()]

    def _preview_reprefix(self):
        refs = self._selected_refs()
        if not refs:
            messagebox.showinfo("Preview", "Select one or more images first.")
            return
        prefix = self.prefix_var.get()
        keep = self.keep_old_reg_var.get()
        self._log("── re-tag preview ──")
        for ref in refs:
            self._log(f"  {ref}  →  {docker_ops.reprefix(ref, prefix, keep)}")

    def _reprefix_selected(self, push: bool):
        refs = self._selected_refs()
        if not refs:
            messagebox.showwarning("Tag", "Select one or more images first.")
            return
        prefix = self.prefix_var.get().strip()
        if not prefix:
            messagebox.showwarning("Tag", "Enter a new prefix.")
            return
        keep = self.keep_old_reg_var.get()
        steps = []
        for ref in refs:
            dst = docker_ops.reprefix(ref, prefix, keep)
            steps.append((f"tag {ref} → {dst}", docker_ops.tag_argv(ref, dst), None))
            if push:
                steps.append((f"push {dst}", docker_ops.push_argv(dst), None))
        self._start(steps, after=self._refresh_images)

    def _push_selected(self):
        refs = [r for r in self._selected_refs() if ":" in r or "/" in r]
        if not refs:
            messagebox.showwarning("Push", "Select named images (repo:tag) to push.")
            return
        self._start([(f"push {r}", docker_ops.push_argv(r), None) for r in refs])

    def _remove_selected(self):
        refs = self._selected_refs()
        if not refs:
            return
        if not messagebox.askyesno("Remove",
                                   f"Run `docker rmi` on {len(refs)} image(s)?"):
            return
        self._start([(f"rmi {r}", docker_ops.rmi_argv(r), None) for r in refs],
                    after=self._refresh_images)

    # -- Load & Push tab ----------------------------------------------------
    def _build_push_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Load & Push")
        self._screens["Load & Push"] = tab

        ttk.Label(tab, text="On the air-gapped side: load the .tar(s), retag for the "
                  "destination registry, then push.", foreground="#555").pack(anchor="w")

        tf = ttk.Frame(tab)
        tf.pack(fill="x", pady=(10, 2))
        ttk.Label(tf, text="Tar file(s):").pack(side="left")
        self.tars_var = tk.StringVar()
        ttk.Entry(tf, textvariable=self.tars_var).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(tf, text="Browse…", command=self._browse_tars).pack(side="left")
        b = ttk.Button(tf, text="📥 Load tar(s)", command=self._do_load)
        b.pack(side="left", padx=4)
        self._action_btns.append(b)

        dest = ttk.Frame(tab)
        dest.pack(fill="x", pady=(10, 2))
        ttk.Label(dest, text="Destination registry (ACR):").pack(side="left")
        self.dest_var = tk.StringVar(value="myregistry.azurecr.io")
        ttk.Entry(dest, textvariable=self.dest_var, width=34).pack(side="left", padx=4)
        self.keep_ns_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(dest, text="keep repo namespace",
                        variable=self.keep_ns_var).pack(side="left", padx=6)

        ttk.Label(tab, text="Images to retag & push (one per line — usually the same "
                  "list you pulled):").pack(anchor="w", pady=(10, 2))
        self.push_txt = scrolledtext.ScrolledText(tab, height=9, wrap="none")
        self.push_txt.pack(fill="both", expand=True)

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="⟸ copy from Pull tab",
                   command=self._copy_from_pull).pack(side="left")
        ttk.Button(bar, text="Preview retags", command=self._preview_retags).pack(
            side="left", padx=6)

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=(8, 0))
        b = ttk.Button(act, text="🏷️ Retag & Push", command=self._do_retag_push)
        b.pack(side="left", padx=4)
        self._action_btns.append(b)

    def _browse_tars(self):
        paths = filedialog.askopenfilenames(
            title="Select tar file(s)", filetypes=[("Tar", "*.tar"), ("All", "*.*")])
        if paths:
            self.tars_var.set(os.pathsep.join(paths))

    def _copy_from_pull(self):
        self.push_txt.delete("1.0", "end")
        self.push_txt.insert("1.0", "\n".join(self._current_images()))

    def _push_images(self) -> list[str]:
        return docker_ops.parse_images(self.push_txt.get("1.0", "end"))

    def _preview_retags(self):
        dest = self.dest_var.get().strip()
        keep = self.keep_ns_var.get()
        imgs = self._push_images()
        if not imgs:
            messagebox.showinfo("Preview", "No images listed.")
            return
        self._log("── retag preview ──")
        for ref in imgs:
            self._log(f"  {ref}  →  {docker_ops.retag_for(ref, dest, keep)}")

    def _do_load(self):
        raw = self.tars_var.get().strip()
        if not raw:
            messagebox.showwarning("Load", "Pick one or more .tar files.")
            return
        steps = [(f"load {Path(p).name}", docker_ops.load_argv(p), None)
                 for p in raw.split(os.pathsep) if p]
        self._start(steps)

    def _do_retag_push(self):
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showwarning("Push", "Enter the destination registry.")
            return
        imgs = self._push_images()
        if not imgs:
            messagebox.showwarning("Push", "List the images to retag & push.")
            return
        keep = self.keep_ns_var.get()
        steps = []
        for ref in imgs:
            dst = docker_ops.retag_for(ref, dest, keep)
            steps.append((f"tag {ref} → {dst}", docker_ops.tag_argv(ref, dst), None))
            steps.append((f"push {dst}", docker_ops.push_argv(dst), None))
        self._start(steps)

    # -- Helm Charts screen -------------------------------------------------
    def _build_helm_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Helm Charts")
        self._screens["Helm Charts"] = tab

        # --- repos row ---
        repos = ttk.LabelFrame(tab, text="Repositories", padding=6)
        repos.pack(fill="x")
        self.repo_tree = ttk.Treeview(repos, columns=("name", "url"),
                                      show="headings", height=3)
        self.repo_tree.heading("name", text="Name")
        self.repo_tree.column("name", width=140, stretch=False)
        self.repo_tree.heading("url", text="URL")
        self.repo_tree.column("url", width=560)
        self.repo_tree.pack(fill="x")
        rr = ttk.Frame(repos)
        rr.pack(fill="x", pady=(6, 0))
        ttk.Label(rr, text="Add — name:").pack(side="left")
        self.repo_name_var = tk.StringVar()
        ttk.Entry(rr, textvariable=self.repo_name_var, width=14).pack(side="left", padx=4)
        ttk.Label(rr, text="url:").pack(side="left")
        self.repo_url_var = tk.StringVar()
        ttk.Entry(rr, textvariable=self.repo_url_var, width=40).pack(side="left", padx=4)
        b = ttk.Button(rr, text="Add repo", command=self._add_repo)
        b.pack(side="left", padx=4)
        self._action_btns.append(b)
        b = ttk.Button(rr, text="⟳ Update repos", command=self._update_repos)
        b.pack(side="left")
        self._action_btns.append(b)

        # --- search row ---
        srch = ttk.Frame(tab)
        srch.pack(fill="x", pady=(10, 2))
        ttk.Label(srch, text="Search charts:").pack(side="left")
        self.helm_query_var = tk.StringVar()
        e = ttk.Entry(srch, textvariable=self.helm_query_var, width=30)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self._search_charts())
        b = ttk.Button(srch, text="🔎 Search (all versions)", command=self._search_charts)
        b.pack(side="left")
        self._action_btns.append(b)
        self.helm_count = ttk.Label(srch, text="", foreground="#777")
        self.helm_count.pack(side="right")

        cols = ("chart", "version", "app_version", "description")
        tree = ttk.Treeview(tab, columns=cols, show="headings",
                            selectmode="extended", height=11)
        for c, w in (("chart", 220), ("version", 110), ("app_version", 110),
                     ("description", 430)):
            tree.heading(c, text=c.replace("_", " ").title())
            tree.column(c, width=w)
        tree.pack(fill="both", expand=True, pady=6)
        self.helm_tree = tree

        # --- dest + actions ---
        dest = ttk.Frame(tab)
        dest.pack(fill="x")
        ttk.Label(dest, text="Charts folder:").pack(side="left")
        self.chartdir_var = tk.StringVar(value=str(Path.cwd() / "helm-charts"))
        ttk.Entry(dest, textvariable=self.chartdir_var).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(dest, text="Browse…", command=lambda: self._browse_into(
            self.chartdir_var)).pack(side="left")

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=(8, 0))
        for text, cmd in (("⬇ Pull chart(s)", self._pull_charts),
                          ("🔍 List images in selected", self._list_chart_images)):
            b = ttk.Button(act, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self._action_btns.append(b)

        # --- push a chart tgz to OCI (ACR) ---
        push = ttk.LabelFrame(tab, text="Push chart to OCI registry (ACR)", padding=6)
        push.pack(fill="x", pady=(10, 0))
        ttk.Label(push, text="Chart .tgz:").grid(row=0, column=0, sticky="e")
        self.chart_tgz_var = tk.StringVar()
        ttk.Entry(push, textvariable=self.chart_tgz_var, width=46).grid(
            row=0, column=1, padx=4, sticky="we")
        ttk.Button(push, text="Browse…", command=self._browse_tgz).grid(row=0, column=2)
        ttk.Label(push, text="OCI target:").grid(row=1, column=0, sticky="e")
        self.oci_target_var = tk.StringVar(value="oci://myacr.azurecr.io/helm")
        ttk.Entry(push, textvariable=self.oci_target_var, width=46).grid(
            row=1, column=1, padx=4, sticky="we")
        push.columnconfigure(1, weight=1)
        ttk.Label(push, text="Log in via the Registry login box at the top "
                  "(docker/az login populates the store helm OCI push uses).",
                  foreground="#777").grid(row=2, column=1, sticky="w", pady=(2, 4))
        bb = ttk.Frame(push)
        bb.grid(row=3, column=1, sticky="w", pady=(2, 0))
        b = ttk.Button(bb, text="⬆ Push chart", command=self._push_chart)
        b.pack(side="left")
        self._action_btns.append(b)

    def _refresh_repos(self):
        threading.Thread(target=self._repos_worker, daemon=True).start()

    def _repos_worker(self):
        try:
            code, out = docker_ops.run_capture(helm_ops.repo_list_argv())
        except FileNotFoundError:
            return
        self._helm_q.put(("repos", helm_ops.parse_repo_list(out) if code == 0 else []))

    def _add_repo(self):
        name = self.repo_name_var.get().strip()
        url = self.repo_url_var.get().strip()
        if not name or not url:
            messagebox.showwarning("Add repo", "Enter both a name and a URL.")
            return
        self._run_now([(f"repo add {name}", helm_ops.repo_add_argv(name, url), None)],
                      after=self._refresh_repos)

    def _update_repos(self):
        self._run_now([("repo update", helm_ops.repo_update_argv(), None)],
                      after=self._refresh_repos)

    def _search_charts(self):
        kw = self.helm_query_var.get().strip()
        if not kw:
            messagebox.showinfo("Search", "Enter a chart name or keyword.")
            return
        self.status.config(text=f"Searching charts for “{kw}”…")
        threading.Thread(target=self._search_worker, args=(kw,), daemon=True).start()

    def _search_worker(self, kw):
        try:
            code, out = docker_ops.run_capture(helm_ops.search_argv(kw, versions=True))
        except FileNotFoundError:
            self._helm_q.put(("err", "helm not found on PATH."))
            return
        self._helm_q.put(("versions", helm_ops.parse_search(out) if code == 0 else []))

    def _populate_helm(self):
        self.helm_tree.delete(*self.helm_tree.get_children(""))
        for i, r in enumerate(self._helm_versions):
            self.helm_tree.insert("", "end", iid=str(i),
                                  values=(r["chart"], r["version"],
                                          r["app_version"], r["description"]))
        self.helm_count.config(text=f"{len(self._helm_versions)} version(s)")

    def _selected_charts(self) -> list[dict]:
        return [self._helm_versions[int(i)] for i in self.helm_tree.selection()]

    def _pull_charts(self):
        sel = self._selected_charts()
        if not sel:
            messagebox.showwarning("Pull chart", "Select one or more chart versions.")
            return
        dest = self.chartdir_var.get().strip() or "."
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Charts folder", str(e))
            return
        steps = []
        for r in sel:
            steps.append((f"pull {r['chart']} {r['version']} → {helm_ops.tgz_name(r['chart'], r['version'])}",
                          helm_ops.pull_argv(r["chart"], r["version"], dest), None))
        self._start(steps, after=self._refresh_files)

    def _list_chart_images(self):
        sel = self._selected_charts()
        if not sel:
            messagebox.showwarning("List images", "Select a chart version first.")
            return
        self.status.config(text="Rendering chart(s) to find images…")
        threading.Thread(target=self._chart_images_worker, args=(sel,),
                         daemon=True).start()

    def _chart_images_worker(self, sel):
        found = []
        for r in sel:
            try:
                code, out = docker_ops.run_capture(
                    helm_ops.template_argv(r["chart"], r["version"]))
            except FileNotFoundError:
                self._helm_q.put(("err", "helm not found on PATH."))
                return
            if code == 0:
                found += helm_ops.extract_images(out)
            else:
                self._helm_q.put(("line", f"⚠ template failed for {r['chart']} "
                                          f"{r['version']}: {out.strip()[:200]}"))
        # de-dupe preserving order
        seen, uniq = set(), []
        for im in found:
            if im not in seen:
                seen.add(im)
                uniq.append(im)
        self._helm_q.put(("images", uniq))

    def _push_chart(self):
        tgz = self.chart_tgz_var.get().strip()
        target = self.oci_target_var.get().strip()
        if not tgz or not target:
            messagebox.showwarning("Push chart", "Pick a .tgz and an oci:// target.")
            return
        self._start([(f"push {Path(tgz).name} → {target}",
                      helm_ops.push_argv(tgz, target), None)])

    def _browse_tgz(self):
        p = filedialog.askopenfilename(title="Select chart .tgz",
                                       filetypes=[("Helm chart", "*.tgz"), ("All", "*.*")])
        if p:
            self.chart_tgz_var.set(p)

    def _browse_into(self, var):
        d = filedialog.askdirectory(title="Choose folder")
        if d:
            var.set(d)

    # -- Files screen (simple directory browser) ----------------------------
    def _build_files_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Files")
        self._screens["Files"] = tab

        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Root:").pack(side="left")
        self.files_root_var = tk.StringVar(value=str(Path.cwd()))
        ttk.Entry(top, textvariable=self.files_root_var).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Browse…",
                   command=lambda: (self._browse_into(self.files_root_var),
                                    self._refresh_files())).pack(side="left")
        ttk.Button(top, text="⟳", width=3, command=self._refresh_files).pack(side="left")

        tree = ttk.Treeview(tab, columns=("size", "modified"), height=18)
        tree.heading("#0", text="Name")
        tree.column("#0", width=520)
        tree.heading("size", text="Size")
        tree.column("size", width=110, anchor="e")
        tree.heading("modified", text="Modified")
        tree.column("modified", width=160)
        tree.pack(fill="both", expand=True, pady=6)
        # Lazy expansion: fill a folder's children the first time it's opened.
        tree.bind("<<TreeviewOpen>>", self._on_file_open)
        self.files_tree = tree

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open in file manager",
                   command=self._open_in_manager).pack(side="left")

    def _fmt_size(self, n: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _insert_dir(self, parent, path: Path):
        """List `path` into `parent`; folders get a dummy child so they expand."""
        try:
            entries = sorted(path.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for p in entries:
            try:
                st = p.stat()
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                size = "" if p.is_dir() else self._fmt_size(st.st_size)
            except OSError:
                mtime, size = "", ""
            icon = "📁 " if p.is_dir() else "📄 "
            node = self.files_tree.insert(parent, "end", text=icon + p.name,
                                          values=(size, mtime), open=False)
            self.files_tree.item(node, tags=(str(p),))
            if p.is_dir():
                self.files_tree.insert(node, "end", text="…")  # expand placeholder

    def _on_file_open(self, _e=None):
        node = self.files_tree.focus()
        tags = self.files_tree.item(node, "tags")
        if not tags:
            return
        path = Path(tags[0])
        kids = self.files_tree.get_children(node)
        # Replace the placeholder on first open.
        if len(kids) == 1 and not self.files_tree.item(kids[0], "tags"):
            self.files_tree.delete(kids[0])
            self._insert_dir(node, path)

    def _refresh_files(self):
        if not hasattr(self, "files_tree"):
            return
        self.files_tree.delete(*self.files_tree.get_children(""))
        root = Path(self.files_root_var.get().strip() or ".")
        if root.exists():
            self._insert_dir("", root)

    def _open_in_manager(self):
        sel = self.files_tree.focus()
        tags = self.files_tree.item(sel, "tags") if sel else ()
        target = Path(tags[0]) if tags else Path(self.files_root_var.get())
        folder = target if target.is_dir() else target.parent
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as e:
            messagebox.showerror("Open", str(e))

    # -- Preview / Run tab (dry-run staging — nothing runs until confirmed) -
    def _build_preview_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Preview / Run")
        self._screens["Preview / Run"] = tab
        self._preview_tab = tab

        ttk.Label(tab, text="Every action stages its exact docker commands here "
                  "first. Nothing runs until you press “Run plan”.",
                  foreground="#555").pack(anchor="w")

        cols = ("#", "action", "command")
        tree = ttk.Treeview(tab, columns=cols, show="headings", height=14)
        tree.heading("#", text="#")
        tree.column("#", width=38, anchor="e", stretch=False)
        tree.heading("action", text="Action")
        tree.column("action", width=240, stretch=False)
        tree.heading("command", text="Command (as it will run)")
        tree.column("command", width=680)
        tree.pack(fill="both", expand=True, pady=6)
        self.plan_tree = tree

        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        self.run_plan_btn = ttk.Button(bar, text="▶ Run plan",
                                       command=self._run_plan, state="disabled")
        self.run_plan_btn.pack(side="left")
        ttk.Button(bar, text="Clear plan", command=self._clear_plan).pack(
            side="left", padx=6)
        self.plan_label = ttk.Label(bar, text="No plan staged.", foreground="#777")
        self.plan_label.pack(side="left", padx=12)

    def _render_plan(self):
        self.plan_tree.delete(*self.plan_tree.get_children(""))
        for i, (label, argv, _stdin) in enumerate(self._pending_steps, 1):
            self.plan_tree.insert("", "end",
                                  values=(i, label, docker_ops.format_command(argv)))
        n = len(self._pending_steps)
        self.plan_label.config(
            text=(f"{n} command(s) staged — review, then Run plan."
                  if n else "No plan staged."))
        self.run_plan_btn.config(state=("normal" if n and not self.runner.busy
                                        else "disabled"))

    def _clear_plan(self):
        self._pending_steps = []
        self._pending_after = None
        self._render_plan()

    def _run_plan(self):
        if self.runner.busy:
            messagebox.showinfo("Busy", "A job is already running.")
            return
        if not self._pending_steps:
            return
        steps = self._pending_steps
        self._after_job = self._pending_after
        self._pending_steps = []
        self._pending_after = None
        self._render_plan()
        self._set_busy(True)
        self.runner.run_steps(steps)

    def _run_now(self, steps, after=None):
        """Run local/read-only steps immediately (bypassing the plan).

        Used for safe, non-ACR operations like `helm repo update` — the plan
        gate is reserved for the things that actually pull/push/produce artifacts.
        """
        if self.runner.busy:
            messagebox.showinfo("Busy", "A job is already running.")
            return
        if not steps:
            return
        self._after_job = after
        self._set_busy(True)
        self.runner.run_steps(steps)

    # -- shared log pane + job plumbing ------------------------------------
    def _build_log_pane(self):
        f = ttk.LabelFrame(self, text="Output", padding=4)
        f.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = scrolledtext.ScrolledText(f, height=12, wrap="word",
                                             state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        bottom = ttk.Frame(f)
        bottom.pack(fill="x", pady=(4, 0))
        self.status = ttk.Label(bottom, text="Ready.")
        self.status.pack(side="left")
        ttk.Button(bottom, text="Clear", command=self._clear_log).pack(side="right")

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _log(self, text: str):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for b in self._action_btns:
            b.config(state=state)
        self.run_plan_btn.config(
            state=("disabled" if busy or not self._pending_steps else "normal"))
        self.status.config(text="Working…" if busy else "Ready.")

    def _start(self, steps, after=None):
        """Stage `steps` into the Preview tab instead of running them directly.

        The user reviews the exact commands there and presses Run plan to execute
        — so nothing (pull/tag/push/rmi/login) ever runs unverified. `after` is a
        callable run once the plan completes (e.g. refresh the image list).
        """
        if self.runner.busy:
            messagebox.showinfo("Busy", "A job is already running.")
            return
        if not steps:
            return
        self._pending_steps = steps
        self._pending_after = after
        self._render_plan()
        self.nb.select(self._preview_tab)
        self._log(f"Staged {len(steps)} command(s) → review in the Preview / Run tab.")

    def _drain(self):
        """UI-thread timer: pull job messages off the queue and render them."""
        try:
            while True:
                kind, text = self._q.get_nowait()
                if kind == "step":
                    self._log(f"\n▶ {text}")
                elif kind == "cmd":
                    self._log(f"  $ {text}")
                elif kind == "line":
                    self._log(f"    {text}")
                elif kind == "ok":
                    self._log(f"  ✓ {text}")
                elif kind == "done":
                    self._set_busy(False)
                    if text:
                        self._log(f"\n✗ stopped at: {text}")
                    else:
                        self._log("\n✓ all steps completed.")
                    cb, self._after_job = self._after_job, None
                    if cb:
                        cb()
        except queue.Empty:
            pass
        # Drain the Docker-status check (the red/green banner at the top).
        try:
            while True:
                kind, detail = self._status_q.get_nowait()
                if kind == "up":
                    self.docker_dot.config(fg="#2e9e44")
                    self.docker_status_lbl.config(text="Docker connected")
                    self.docker_detail_lbl.config(
                        text=f"server v{detail}  ·  {docker_ops.docker_path()}")
                elif kind == "down":
                    self.docker_dot.config(fg="#d33")
                    self.docker_status_lbl.config(text="Docker not available")
                    self.docker_detail_lbl.config(text=detail)
        except queue.Empty:
            pass
        # Drain the images-list worker too.
        try:
            while True:
                kind, payload = self._img_q.get_nowait()
                if kind == "rows":
                    self._img_rows = payload
                    self._populate_images()
                    self.status.config(text=f"Loaded {len(payload)} images.")
                elif kind == "err":
                    self.status.config(text="Ready.")
                    self._log(f"⚠ {payload}")
        except queue.Empty:
            pass
        # Drain the helm worker (repos / search / chart-image discovery).
        try:
            while True:
                kind, payload = self._helm_q.get_nowait()
                if kind == "repos":
                    self.repo_tree.delete(*self.repo_tree.get_children(""))
                    for r in payload:
                        self.repo_tree.insert("", "end", values=(r["name"], r["url"]))
                elif kind == "versions":
                    self._helm_versions = payload
                    self._populate_helm()
                    self.status.config(text=f"Found {len(payload)} chart version(s).")
                elif kind == "images":
                    self.status.config(text=f"Chart references {len(payload)} image(s).")
                    self._log("── images referenced by selected chart(s) ──")
                    for im in payload:
                        self._log(f"    {im}")
                    if payload:
                        self._append_pull_images(payload)
                        self._log(f"→ added {len(payload)} image(s) to the Pull & Save "
                                  "list for review.")
                elif kind == "line":
                    self._log(payload)
                elif kind == "err":
                    self.status.config(text="Ready.")
                    self._log(f"⚠ {payload}")
        except queue.Empty:
            pass
        self.after(120, self._drain)


if __name__ == "__main__":
    App().mainloop()
