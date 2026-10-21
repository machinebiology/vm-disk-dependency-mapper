#!/usr/bin/env python3
"""
vm_disk_purge_gui.py - Choose virtual machine disk images (.vdi and .vmdk) to
purge, and see which other images a purge would break.

Images are shown in groups. A group is a base image plus every snapshot delta
built on it. Groups that contain deltas are marked as needing consolidation:
the newest data lives in the deltas, so the chain should be merged into one
image before files are extracted from it.

Click the "Purge" column to check a file. Any image that relies on a checked
file, but is not checked itself, is highlighted because it would stop working.
Checked files can be exported as a list. This tool never deletes, moves or
modifies any disk image or VM file; it only reads them.

Usage:
    python vm_disk_purge_gui.py
    python vm_disk_purge_gui.py D:\\VMs E:\\VirtualBox     (scans immediately)

Requires vm_disk_deps.py in the same folder.
"""

import json
import os
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

import vm_disk_deps as deps

BOX = {True: "[X]", False: "[ ]", None: "[-]"}   # None = some members checked
SELECTION_FILE_VERSION = 1


# --------------------------------------------------------------------------
# Purge plan (no GUI code, so it can be tested on its own)
# --------------------------------------------------------------------------

class FileList:
    """A pseudo-group for files that are not part of any image chain:
    orphaned VMDK data files and files that could not be read."""

    def __init__(self, key, title, members, note):
        self.key = key
        self.title = title
        self.members = sorted(members, key=lambda m: m.path.lower())
        self.tops = self.members
        self.note = note
        self.file_size = sum(file_size(m) or 0 for m in self.members)

    def notes(self):
        return [self.note]


def file_size(img):
    if img.file_size is None:
        img.file_size = deps.getsize(img.path)
    return img.file_size


class PurgePlan:
    def __init__(self, analysis):
        self.analysis = analysis
        self.groups = list(analysis.groups)
        self.lists = []
        if analysis.orphan_extents:
            self.lists.append(FileList(
                "list:orphans", "VMDK data files with no descriptor",
                analysis.orphan_extents,
                "These files hold disk data, but no .vmdk descriptor that uses them "
                "was found in the scanned folders. Without the descriptor they cannot "
                "be opened by VirtualBox or VMware. The descriptor may be in a folder "
                "that was not scanned."))
        if analysis.errors:
            self.lists.append(FileList(
                "list:errors", "Files that could not be read", analysis.errors,
                "These files could not be read, so their dependencies are unknown. A "
                "file can be unreadable because a running VM has it locked, because "
                "of permissions, or because it is damaged."))
        self.items = {}
        for g in self.groups + self.lists:
            for m in g.members:
                self.items[m.key] = m
        self.checked = set()        # keys of images checked for purge
        self.extracted = set()      # keys of groups marked "files extracted"

    # --- checking -------------------------------------------------------

    def is_checked(self, img):
        return img.key in self.checked

    def set_checked(self, imgs, value):
        for img in imgs:
            if value:
                self.checked.add(img.key)
            else:
                self.checked.discard(img.key)

    def group_state(self, g):
        n = sum(1 for m in g.members if m.key in self.checked)
        if n == 0:
            return False
        return True if n == len(g.members) else None

    # --- dependency effects --------------------------------------------

    def broken_by(self, img):
        """The nearest checked image that this unchecked image relies on."""
        if img.key in self.checked:
            return None
        for a in deps.ancestors(img):
            if a.key in self.checked:
                return a
        return None

    def broken_images(self):
        return [m for g in self.groups for m in g.members if self.broken_by(m)]

    @staticmethod
    def dependents(img):
        """Every image that relies on img, directly or indirectly."""
        return [d for d, _ in deps.descendants(img)]

    # --- what will be purged -------------------------------------------

    def checked_images(self):
        return [m for g in self.groups + self.lists for m in g.members
                if m.key in self.checked]

    @staticmethod
    def files_of(img):
        """The image file plus any separate VMDK data files it owns."""
        return [img.path] + img.data_files

    def files_to_purge(self):
        out = {}
        for img in self.checked_images():
            for p in self.files_of(img):
                out.setdefault(deps.norm_path(p), p)
        return list(out.values())

    def purge_size(self):
        return sum(file_size(m) or 0 for m in self.checked_images())

    def unextracted_groups(self):
        """Groups with checked images whose files are not marked as extracted.
        Groups whose base is missing are skipped, as they cannot be opened."""
        return [g for g in self.groups
                if self.group_state(g) is not False and g.key not in self.extracted
                and not g.base_missing]

    # --- text -----------------------------------------------------------

    def status_text(self, img):
        if img.key in self.checked:
            return "PURGE"
        b = self.broken_by(img)
        if b:
            return f"WILL BREAK - relies on {os.path.basename(b.path)}"
        if img.error:
            return f"could not be read: {img.error}"
        if img.role == "extent":
            return "data file with no descriptor"
        g = img.group
        if g and g.base_missing:
            return "unusable - base image missing"
        if not img.wants_parent:
            return "base image (has deltas)" if img.children else "base image"
        return "snapshot delta (end state)" if not img.children else "snapshot delta"

    def group_status_text(self, g):
        parts = []
        broken = [m for m in g.members if self.broken_by(m)]
        if broken:
            parts.append(f"{len(broken)} image(s) WILL BREAK")
        state = self.group_state(g)
        if state is True:
            parts.append("PURGE ALL")
        elif state is None:
            n = sum(1 for m in g.members if m.key in self.checked)
            parts.append(f"{n} of {len(g.members)} checked")
        if isinstance(g, FileList):
            return "; ".join(parts) or f"{len(g.members)} file(s)"
        if g.needs_consolidation:
            parts.append(f"NEEDS CONSOLIDATION ({len(g.deltas)} delta(s), "
                         f"{len(g.end_states)} end state(s))")
        elif g.base_missing:
            parts.append("base image missing")
        else:
            parts.append("single image")
        return "; ".join(parts)

    def summary_text(self):
        imgs = self.checked_images()
        if not imgs:
            return "Nothing checked for purge.", False
        files = self.files_to_purge()
        broken = self.broken_images()
        text = (f"Checked: {len(imgs)} image(s), {len(files)} file(s), "
                f"{deps.human(self.purge_size())} would be freed.")
        if broken:
            text += (f"  WARNING: {len(broken)} unchecked image(s) rely on checked "
                     f"files and would stop working.")
        return text, bool(broken)

    def image_details(self, img):
        """Lines of (text, style) for the details pane. Styles: h = heading,
        w = warning, None = normal."""
        L = []
        add = lambda t, s=None: L.append((t, s))
        add(img.path, "h")
        if img.role == "extent":
            add(f"Type: VMDK data file ({img.kind}), {deps.human(file_size(img))}")
        elif img.error:
            add(f"Could not be read: {img.error}", "w")
        else:
            add(f"Type: {deps.describe(img)}")
        if img.group:
            add(f"Group {img.group.number}: {img.group.title}")
        for r in sorted(set(img.vm_refs)):
            add(f"Used by: {r}")
        for e in img.data_files:
            add(f"Data file (would be purged together with this image): {e}")
        status = self.status_text(img)
        add(f"Status: {status}", "w" if status.startswith(("WILL", "PURGE")) else None)
        if img.role == "extent" or img.error:
            return L

        add("")
        add("RELIES ON", "h")
        anc = deps.ancestors(img)
        for i, a in enumerate(anc, 1):
            mark = "  (checked for purge)" if a.key in self.checked else ""
            add(f"  {deps.relation_name(i, up=True)}: {a.path}{mark}",
                "w" if mark else None)
        top = anc[-1] if anc else img
        if top.wants_parent:
            for line in deps.missing_parent_text(top, {}, self.analysis.registry):
                add(f"  {line}", "w")
        elif not anc:
            add("  nothing (this is a base image)")

        add("")
        add("IF YOU PURGE THIS FILE", "h")
        dep = deps.descendants(img)
        if dep:
            unchecked = [d for d, _ in dep if d.key not in self.checked]
            add(f"  {len(dep)} image(s) that rely on it will stop working"
                + (f" ({len(unchecked)} of them not checked for purge):"
                   if unchecked else " (all of them are checked for purge too):"),
                "w" if unchecked else None)
            for d, gen in dep:
                mark = "  (checked)" if d.key in self.checked else "  (NOT checked)"
                add(f"  {'  ' * gen}{d.path}  [{deps.relation_name(gen, up=False)}]"
                    f"{mark}", None if d.key in self.checked else "w")
        else:
            add("  No other image found in the scanned folders relies on it.")
        if img.vm_refs:
            add("  These VM configurations would report a missing disk:", "w")
            for r in sorted(set(img.vm_refs)):
                add(f"    {r}", "w")
        if img.group and img.group.needs_consolidation and not anc and img.children:
            add("  This is the base image of a group with snapshot deltas. Consolidate "
                "the group before extracting files and purging it.", "w")

        notes = img.link_notes + [f"other candidate parent: {a}" for a in img.alternates]
        notes += [f"missing data file: {m}" for m in img.missing_extents]
        if notes:
            add("")
            add("NOTES", "h")
            for n in notes:
                add(f"  {n}")
        return L

    def group_details(self, g):
        L = []
        add = lambda t, s=None: L.append((t, s))
        if isinstance(g, FileList):
            add(g.title, "h")
        else:
            add(f"Group {g.number}: {g.title}", "h")
        add(f"{len(g.members)} file(s), {deps.human(g.file_size)} on disk in total")
        add(f"Status: {self.group_status_text(g)}")
        if not isinstance(g, FileList):
            add(f"Files extracted: {'yes' if g.key in self.extracted else 'not marked'}")
            for r in g.vm_refs:
                add(f"Used by: {r}")
        add("")
        for n in g.notes():
            add(n, "w" if n.startswith(("Contains", "The chain", "The base", "MISSING"))
                else None)
        if not isinstance(g, FileList):
            add("")
            add("Purging every file in this group does not affect any other group "
                "found in the scanned folders.")
        return L

    def export_text(self):
        out = ["VM DISK PURGE LIST",
               f"Created: {time.strftime('%Y-%m-%d %H:%M:%S')}",
               "Folders scanned:"]
        out += [f"  {f}" for f in self.analysis.folders]
        files = self.files_to_purge()
        out += ["", f"FILES TO PURGE ({len(files)}, {deps.human(self.purge_size())}):"]
        for p in sorted(files, key=str.lower):
            out.append(f"  {p}  ({deps.human(deps.getsize(p))})")
        broken = self.broken_images()
        out += ["", f"IMAGES THAT WOULD STOP WORKING BUT ARE NOT CHECKED ({len(broken)}):"]
        for m in broken:
            out.append(f"  {m.path}  (relies on {self.broken_by(m).path})")
        if not broken:
            out.append("  none")
        unext = self.unextracted_groups()
        out += ["", f"GROUPS WITH CHECKED FILES NOT MARKED AS EXTRACTED ({len(unext)}):"]
        for g in unext:
            out.append(f"  Group {g.number}: {g.title}")
        if not unext:
            out.append("  none")
        used = [(m, r) for m in self.checked_images() for r in sorted(set(m.vm_refs))]
        out += ["", f"CHECKED FILES STILL USED BY A VM CONFIGURATION ({len(used)}):"]
        for m, r in used:
            out.append(f"  {m.path}  ({r})")
        if not used:
            out.append("  none")
        return "\n".join(out) + "\n"

    # --- saving selections ------------------------------------------------

    def to_json(self):
        return {"version": SELECTION_FILE_VERSION,
                "folders": self.analysis.folders,
                "purge": sorted(self.items[k].path for k in self.checked
                                if k in self.items),
                "extracted": sorted(self.items[k].path for k in self.extracted
                                    if k in self.items)}

    def apply_json(self, data):
        """Apply saved selections. Returns the number of saved paths that no
        longer match a scanned file."""
        unmatched = 0
        for field, target in (("purge", self.checked), ("extracted", self.extracted)):
            for p in data.get(field, []):
                k = deps.norm_path(p)
                if k in self.items:
                    target.add(k)
                else:
                    unmatched += 1
        return unmatched


# --------------------------------------------------------------------------
# Progress reporting from the scan thread
# --------------------------------------------------------------------------

class QueueProgress(deps.NullProgress):
    """Sends progress messages to the GUI thread through a queue."""

    def __init__(self, q):
        self.q = q
        self.step = 0
        self.label = ""
        self.last = 0.0

    def start(self, step, label):
        self.step, self.label = step, label
        self._put(0.0, f"{label}...", True)

    def update(self, text, force=False):
        self._put(None, f"{self.label}: {text}", force)

    def counter(self, i, n, noun):
        self._put(i / max(n, 1), f"{self.label}: {i}/{n} {noun}", i == n)

    def done(self, text):
        self._put(1.0, f"{self.label}: {text}", True)

    def _put(self, frac, text, force):
        now = time.monotonic()
        if not force and now - self.last < 0.1:
            return
        self.last = now
        self.q.put(("progress", self.step, frac, text))


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App:
    COLUMNS = (("purge", "Purge", 55, "center"),
               ("extracted", "Extracted", 70, "center"),
               ("status", "Status", 400, "w"),
               ("size", "Size on disk", 95, "e"),
               ("used_by", "Used by", 250, "w"))

    def __init__(self, root, folders):
        self.root = root
        self.plan = None
        self.rows = {}          # tree row id -> ("group" | "image", object)
        self.row_of = {}        # id(image) -> tree row id
        self.dirty = False
        self.scanning = False
        self.q = queue.Queue()
        self.selection_file = None
        root.title("VM Disk Purge Planner")
        root.geometry("1250x800")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        for f in folders:
            self.folder_list.insert("end", os.path.abspath(f))
        if folders:
            root.after(200, self.start_scan)

    # --- layout ------------------------------------------------------------

    def _build(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Folders to scan (searched recursively):").grid(
            row=0, column=0, sticky="w")
        self.folder_list = tk.Listbox(top, height=4, selectmode="extended")
        self.folder_list.grid(row=1, column=0, rowspan=3, sticky="nsew")
        top.columnconfigure(0, weight=1)
        ttk.Button(top, text="Add folder...", command=self.add_folder).grid(
            row=1, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Remove folder", command=self.remove_folder).grid(
            row=2, column=1, sticky="ew", padx=4)
        self.scan_btn = ttk.Button(top, text="Scan", command=self.start_scan)
        self.scan_btn.grid(row=3, column=1, sticky="ew", padx=4)

        prog = ttk.Frame(self.root, padding=(6, 0))
        prog.pack(fill="x")
        self.progress = ttk.Progressbar(prog, maximum=100)
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="Add folders, then click Scan.")
        ttk.Label(prog, textvariable=self.status_var).pack(fill="x")

        legend = ("Click a box in the Purge column (or select rows and press Space) to "
                  "check files. Checking a group row checks all of its files. "
                  "Red rows will be purged. Amber rows rely on a checked file and "
                  "would stop working.")
        ttk.Label(self.root, text=legend, padding=(6, 4), wraplength=1200,
                  foreground="#444").pack(fill="x")

        panes = ttk.PanedWindow(self.root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=6)

        tree_frame = ttk.Frame(panes)
        self.tree = ttk.Treeview(tree_frame, columns=[c[0] for c in self.COLUMNS],
                                 selectmode="extended")
        self.tree.heading("#0", text="Group / image file")
        self.tree.column("#0", width=380, stretch=True)
        for cid, text, width, anchor in self.COLUMNS:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor,
                             stretch=cid in ("status", "used_by"))
        ys = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        panes.add(tree_frame, weight=3)

        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.tree.tag_configure("group", font=bold)
        self.tree.tag_configure("purge", background="#f4b6b6")
        self.tree.tag_configure("break", background="#ffd966")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", self.on_space)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())

        det_frame = ttk.Frame(panes)
        self.details = tk.Text(det_frame, height=12, wrap="word", state="disabled",
                               padx=6, pady=4)
        dys = ttk.Scrollbar(det_frame, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=dys.set)
        self.details.pack(side="left", fill="both", expand=True)
        dys.pack(side="right", fill="y")
        self.details.tag_configure("h", font=bold)
        self.details.tag_configure("w", foreground="#b00000")
        panes.add(det_frame, weight=1)

        bottom = ttk.Frame(self.root, padding=6)
        bottom.pack(fill="x")
        self.summary_lbl = ttk.Label(bottom, text="", wraplength=1200)
        self.summary_lbl.pack(fill="x", pady=(0, 4))
        row1 = ttk.Frame(bottom)
        row1.pack(fill="x", pady=(0, 4))
        row2 = ttk.Frame(bottom)
        row2.pack(fill="x")
        self.action_buttons = []
        for frame, text, cmd in (
                (row1, "Also check images that rely on selection", self.check_dependents),
                (row1, "Toggle 'files extracted' for group", self.toggle_extracted),
                (row1, "Uncheck all", self.uncheck_all),
                (row2, "Save selections...", self.save_selections),
                (row2, "Load selections...", self.load_selections),
                (row2, "Write dependency report...", self.write_report),
                (row2, "Export purge list...", self.export_list)):
            b = ttk.Button(frame, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.action_buttons.append(b)
        self._set_actions_enabled(False)

    def _set_actions_enabled(self, enabled):
        for b in self.action_buttons:
            b.state(["!disabled"] if enabled else ["disabled"])

    # --- folders and scanning ------------------------------------------------

    def add_folder(self):
        d = filedialog.askdirectory(parent=self.root, title="Folder to scan")
        if d:
            d = os.path.abspath(d)
            if d not in self.folder_list.get(0, "end"):
                self.folder_list.insert("end", d)

    def remove_folder(self):
        for i in reversed(self.folder_list.curselection()):
            self.folder_list.delete(i)

    def start_scan(self):
        if self.scanning:
            return
        folders = list(self.folder_list.get(0, "end"))
        if not folders:
            messagebox.showinfo("Nothing to scan", "Add at least one folder first.",
                                parent=self.root)
            return
        self.scanning = True
        self.scan_btn.state(["disabled"])
        self._set_actions_enabled(False)
        self.progress["value"] = 0
        threading.Thread(target=self._scan_worker, args=(folders,), daemon=True).start()
        self.root.after(100, self._poll)

    def _scan_worker(self, folders):
        try:
            self.q.put(("done", deps.analyze(folders, QueueProgress(self.q))))
        except Exception:
            self.q.put(("error", traceback.format_exc()))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, step, frac, text = msg
                    if frac is not None:
                        total = deps.NullProgress.TOTAL_STEPS
                        self.progress["value"] = 100 * (step - 1 + frac) / total
                    self.status_var.set(f"Step {step} of "
                                        f"{deps.NullProgress.TOTAL_STEPS}: {text}")
                elif msg[0] == "done":
                    self._scan_finished(msg[1])
                    return
                elif msg[0] == "error":
                    self.scanning = False
                    self.scan_btn.state(["!disabled"])
                    self.status_var.set("Scan failed.")
                    messagebox.showerror("Scan failed", msg[1], parent=self.root)
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _scan_finished(self, analysis):
        self.scanning = False
        self.scan_btn.state(["!disabled"])
        old = self.plan
        self.plan = PurgePlan(analysis)
        if old:
            # Keep earlier checkmarks for files that still exist.
            self.plan.checked = {k for k in old.checked if k in self.plan.items}
            self.plan.extracted = {k for k in old.extracted if k in self.plan.items}
        self.progress["value"] = 100
        a = analysis
        status = (f"Found {len(a.disks)} disk image(s) in {len(a.groups)} group(s); "
                  f"{sum(1 for g in a.groups if g.needs_consolidation)} group(s) need "
                  f"consolidation, {sum(1 for g in a.groups if g.base_missing)} have a "
                  f"missing base image.")
        if a.warnings:
            status += f" {len(a.warnings)} warning(s), see 'Write dependency report'."
        self.status_var.set(status)
        self._populate()
        self._set_actions_enabled(True)

    # --- tree ------------------------------------------------------------------

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.row_of.clear()
        p = self.plan

        def add_image(parent_row, img):
            row = self.tree.insert(parent_row, "end", text=os.path.basename(img.path),
                                   open=True)
            self.rows[row] = ("image", img)
            self.row_of[id(img)] = row
            for c in sorted(img.children, key=lambda c: c.path.lower()):
                add_image(row, c)

        for g in p.groups + p.lists:
            title = g.title if isinstance(g, FileList) else f"Group {g.number}: {g.title}"
            grow = self.tree.insert("", "end", text=title, open=False)
            self.rows[grow] = ("group", g)
            for t in g.tops:
                if isinstance(g, FileList):
                    row = self.tree.insert(grow, "end", text=t.path)
                    self.rows[row] = ("image", t)
                    self.row_of[id(t)] = row
                else:
                    add_image(grow, t)
        self.refresh()

    def refresh(self):
        p = self.plan
        if p is None:
            return
        for row, (kind, obj) in self.rows.items():
            if kind == "image":
                checked = p.is_checked(obj)
                broken = p.broken_by(obj) is not None
                self.tree.item(row, values=(
                    BOX[checked], "", p.status_text(obj), deps.human(file_size(obj)),
                    "; ".join(sorted(set(obj.vm_refs)))),
                    tags=("purge",) if checked else ("break",) if broken else ())
            else:
                state = p.group_state(obj)
                broken = any(p.broken_by(m) for m in obj.members)
                extracted = ("" if isinstance(obj, FileList) or obj.base_missing
                             else BOX[obj.key in p.extracted])
                used = "" if isinstance(obj, FileList) else "; ".join(obj.vm_refs)
                tags = ["group"]
                if broken:
                    tags.append("break")
                elif state is True:
                    tags.append("purge")
                self.tree.item(row, values=(
                    BOX[state], extracted, p.group_status_text(obj),
                    deps.human(obj.file_size), used), tags=tags)
        text, warning = p.summary_text()
        self.summary_lbl.configure(text=text,
                                   foreground="#b00000" if warning else "")
        self.show_details()

    def show_details(self):
        lines = []
        sel = self.tree.selection()
        if self.plan and sel:
            kind, obj = self.rows[sel[0]]
            lines = (self.plan.image_details(obj) if kind == "image"
                     else self.plan.group_details(obj))
            if len(sel) > 1:
                lines.append(("", None))
                lines.append((f"({len(sel)} rows selected; details shown for the "
                              f"first.)", None))
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        for text, style in lines:
            self.details.insert("end", text + "\n", style or ())
        self.details.configure(state="disabled")

    # --- checking ---------------------------------------------------------------

    def _members(self, row):
        kind, obj = self.rows[row]
        return [obj] if kind == "image" else list(obj.members)

    def toggle_rows(self, rows):
        imgs = []
        for r in rows:
            imgs += self._members(r)
        if not imgs:
            return
        value = not all(self.plan.is_checked(m) for m in imgs)
        before = set(m.key for m in self.plan.broken_images())
        self.plan.set_checked(imgs, value)
        self.dirty = True
        self.refresh()
        newly = [m for m in self.plan.broken_images() if m.key not in before]
        if newly:
            names = ", ".join(os.path.basename(m.path) for m in newly[:3])
            more = f" and {len(newly) - 3} more" if len(newly) > 3 else ""
            self.status_var.set(f"{len(newly)} unchecked image(s) now rely on a checked "
                                f"file and would stop working: {names}{more} "
                                f"(highlighted in amber).")
            self.tree.see(self.row_of[id(newly[0])])

    def on_click(self, event):
        if self.plan is None:
            return
        row = self.tree.identify_row(event.y)
        if (row and self.tree.identify_region(event.x, event.y) == "cell"
                and self.tree.identify_column(event.x) == "#1"):
            self.tree.selection_set(row)
            self.toggle_rows([row])
            return "break"
        if (row and self.tree.identify_region(event.x, event.y) == "cell"
                and self.tree.identify_column(event.x) == "#2"
                and self.rows[row][0] == "group"):
            self.tree.selection_set(row)
            self.toggle_extracted()
            return "break"

    def on_space(self, _event):
        if self.plan is not None:
            self.toggle_rows(self.tree.selection())
        return "break"

    def check_dependents(self):
        imgs = []
        for r in self.tree.selection():
            for m in self._members(r):
                imgs.append(m)
                imgs += self.plan.dependents(m)
        self.plan.set_checked(imgs, True)
        self.dirty = True
        self.refresh()

    def toggle_extracted(self):
        for r in self.tree.selection():
            kind, obj = self.rows[r]
            if kind == "image":
                obj = obj.group
            if obj is None or isinstance(obj, FileList) or obj.base_missing:
                continue
            if obj.key in self.plan.extracted:
                self.plan.extracted.discard(obj.key)
            else:
                self.plan.extracted.add(obj.key)
        self.dirty = True
        self.refresh()

    def uncheck_all(self):
        if self.plan.checked and messagebox.askyesno(
                "Uncheck all", "Remove every purge checkmark?", parent=self.root):
            self.plan.checked.clear()
            self.dirty = True
            self.refresh()

    # --- files ---------------------------------------------------------------

    def _check_output(self, path):
        problem = deps.unsafe_output_reason(path, self.plan.items if self.plan else ())
        if problem:
            messagebox.showerror("Not saved", problem, parent=self.root)
            return False
        return True

    def save_selections(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save selections", defaultextension=".json",
            initialfile=os.path.basename(self.selection_file or "purge_selections.json"),
            filetypes=[("Selection files", "*.json"), ("All files", "*.*")])
        if not path:
            return False
        if not self._check_output(path):
            return False
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.plan.to_json(), f, indent=2)
        self.selection_file = path
        self.dirty = False
        self.status_var.set(f"Selections saved to {path}")
        return True

    def load_selections(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Load selections",
            filetypes=[("Selection files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror("Could not load", str(e), parent=self.root)
            return
        unmatched = self.plan.apply_json(data)
        self.selection_file = path
        self.refresh()
        msg = f"Selections loaded from {path}."
        if unmatched:
            msg += (f" {unmatched} saved path(s) did not match any scanned file; they "
                    f"may have been deleted or be in folders that were not scanned.")
        self.status_var.set(msg)

    def write_report(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Write dependency report", defaultextension=".txt",
            initialfile=deps.DEFAULT_OUTPUT,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path and self._check_output(path):
            lines = deps.build_report(self.plan.analysis, False, deps.NullProgress())
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write("\n".join(lines) + "\n")
            self.status_var.set(f"Dependency report written to {path}")

    def export_list(self):
        if not self.plan.checked:
            messagebox.showinfo("Nothing checked", "No files are checked for purge.",
                                parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export purge list", defaultextension=".txt",
            initialfile="purge_list.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path and self._check_output(path):
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(self.plan.export_text())
            self.status_var.set(f"Purge list written to {path}")

    def on_close(self):
        if self.dirty and self.plan and (self.plan.checked or self.plan.extracted):
            answer = messagebox.askyesnocancel(
                "Unsaved selections", "Save your purge and 'extracted' selections "
                "before closing?", parent=self.root)
            if answer is None or (answer and not self.save_selections()):
                return
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root, sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
