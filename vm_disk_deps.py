#!/usr/bin/env python3
"""
vm_disk_deps.py - For every virtual machine disk image (.vdi or .vmdk) found
in the given folders, list:

  * the image files it RELIES ON: its parent, the parent's parent, and so on
    down to the base image, plus any VMDK data files those images use;
  * the image files that RELY ON IT: its children, grandchildren, and so on.

The report is written to a text file. Progress is shown in the console.

How parents are found (best effort, same method as vm_disk_tree.py):
  * VDI: the header stores the image's own UUID and its parent's UUID.
    Parents are matched by UUID, so a moved parent is still found.
  * VMDK: the descriptor stores parentFileNameHint (a path that is often stale
    after a move) and parentCID (which must equal the parent's CID).
    VMDKs created by VirtualBox also store ddb.uuid.image / ddb.uuid.parent.
  * Candidates are scored using UUID, exact hint path, hint filename,
    filenames from .vbox media registries, and CID consistency. The best
    candidate wins; other candidates are listed so you can check them.
  * .vbox, .vmx and .vmsd files found in the folders are used to label which
    VM (and which snapshot) uses each image.

Usage:
    python vm_disk_deps.py D:\\VMs E:\\Old\\VirtualBox "F:\\Backups\\VMware"
    python vm_disk_deps.py D:\\VMs E:\\ -o C:\\Temp\\disk_report.txt
    python vm_disk_deps.py D:\\VMs -v          (adds UUIDs, CIDs and hints)
"""

import argparse
import os
import re
import struct
import sys
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET
from collections import defaultdict

NULL_UUID = "00000000-0000-0000-0000-000000000000"
VDI_SIGNATURE = 0xBEDA107F
VDI_TYPES = {1: "dynamic", 2: "fixed", 3: "undo", 4: "differencing"}
SECTOR = 512
# Text descriptors are about 1 KB. A header-less .vmdk larger than this is
# treated as a raw data extent (for example a "-flat.vmdk" file).
MAX_DESCRIPTOR_FILE = 1024 * 1024
DEFAULT_OUTPUT = "vm_disk_dependencies.txt"

WARNINGS = []


def warn(msg):
    WARNINGS.append(str(msg))


# --------------------------------------------------------------------------
# Progress display (written to stderr so it never mixes with the report)
# --------------------------------------------------------------------------

class Progress:
    """Prints "[step/total] label: done/count" on one updating line when the
    console supports it, or occasional plain lines otherwise."""

    TOTAL_STEPS = 5

    def __init__(self):
        self.tty = sys.stderr.isatty()
        self.last = 0.0
        self.label = ""
        self.step = 0

    def start(self, step, label):
        self.step, self.label, self.last = step, label, 0.0
        self._emit(f"[{step}/{self.TOTAL_STEPS}] {label}...", force=True)

    def update(self, text, force=False):
        self._emit(f"[{self.step}/{self.TOTAL_STEPS}] {self.label}: {text}", force)

    def done(self, text):
        self._emit(f"[{self.step}/{self.TOTAL_STEPS}] {self.label}: {text}",
                   force=True, final=True)

    def _emit(self, line, force=False, final=False):
        now = time.monotonic()
        if self.tty:
            if not (force or final or now - self.last >= 0.2):
                return
            width = 79
            if len(line) > width and not final:
                line = line[:width - 3] + "..."
            sys.stderr.write("\r" + line.ljust(width) + ("\n" if final else ""))
        else:
            # Redirected output: print a line at most every 5 seconds.
            if not (final or now - self.last >= 5.0):
                return
            sys.stderr.write(line + "\n")
        sys.stderr.flush()
        self.last = now

    def counter(self, i, n, noun):
        self.update(f"{i}/{n} {noun} ({100 * i // max(n, 1)}%)")


PROGRESS = Progress()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def fs(path):
    """Return a path usable for file I/O, adding the Windows long-path prefix
    when the path is close to the 260-character limit."""
    if os.name == "nt":
        p = os.path.abspath(path)
        if len(p) >= 240 and not p.startswith("\\\\?\\"):
            if p.startswith("\\\\"):
                return "\\\\?\\UNC\\" + p[2:]
            return "\\\\?\\" + p
    return path


def norm_path(p):
    return os.path.normcase(os.path.abspath(p))


def basename_any(p):
    """Basename that understands both Windows and POSIX separators, since
    hints may have been written on a different OS."""
    return re.split(r"[\\/]", p.rstrip("\\/"))[-1]


def is_abs_any(p):
    return bool(re.match(r"^([A-Za-z]:[\\/]|[\\/])", p))


def norm_uuid(s):
    if not s:
        return None
    s = s.strip().strip("{}").lower()
    try:
        u = str(uuidlib.UUID(s))
    except ValueError:
        return None
    return None if u == NULL_UUID else u


def uuid_from_le(b):
    """VirtualBox stores UUIDs with the first three fields little-endian."""
    u = str(uuidlib.UUID(bytes_le=bytes(b)))
    return None if u == NULL_UUID else u


def norm_cid(s, is_parent=False):
    if not s:
        return None
    s = s.strip().lower()
    if is_parent and s == "ffffffff":   # ffffffff means "no parent"
        return None
    return s


def human(n):
    if n is None:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def getsize(p):
    try:
        return os.path.getsize(fs(p))
    except OSError:
        return None


def resolve_ref(ref, base_dir):
    """Resolve a path found inside a file, relative to that file's folder."""
    if is_abs_any(ref):
        return ref
    return os.path.join(base_dir, ref.replace("\\", os.sep).replace("/", os.sep))


# --------------------------------------------------------------------------
# Image model
# --------------------------------------------------------------------------

class Image:
    def __init__(self, path):
        self.path = path
        self.key = norm_path(path)
        self.fmt = "VDI" if path.lower().endswith(".vdi") else "VMDK"
        self.role = "disk"          # "disk" or "extent" (VMDK data file)
        self.kind = "?"
        self.uuid = None
        self.parent_uuid = None
        self.modify_uuid = None
        self.parent_modify_uuid = None
        self.cid = None
        self.parent_cid = None
        self.parent_hint = None
        self.virtual_size = None
        self.file_size = None
        self.extents = []           # resolved data file paths (VMDK)
        self.missing_extents = []
        self.owner = None           # descriptor that claims this extent
        self.error = None
        self.parent = None
        self.link_method = None
        self.confidence = None
        self.link_notes = []
        self.verbose_notes = []
        self.alternates = []
        self.children = []
        self.vm_refs = []

    @property
    def wants_parent(self):
        return bool(self.parent_uuid or self.parent_hint or self.parent_cid)

    @property
    def data_files(self):
        """Separate data files (extents) that hold this image's contents."""
        return [e for e in self.extents if norm_path(e) != self.key]


# --------------------------------------------------------------------------
# VDI parsing
# --------------------------------------------------------------------------

def parse_vdi(img):
    with open(fs(img.path), "rb") as f:
        h = f.read(0x200)
    img.file_size = getsize(img.path)
    if len(h) < 0x48 or struct.unpack_from("<I", h, 0x40)[0] != VDI_SIGNATURE:
        img.error = "not a valid VDI (signature not found)"
        return
    major = struct.unpack_from("<I", h, 0x44)[0] >> 16
    if major == 0:
        # Very old header layout (VirtualBox 1.x era).
        if len(h) < 0x1A4:
            img.error = "truncated VDI header"
            return
        typ = struct.unpack_from("<I", h, 0x48)[0]
        img.virtual_size = struct.unpack_from("<Q", h, 0x160)[0]
        img.uuid = uuid_from_le(h[0x174:0x184])
        img.modify_uuid = uuid_from_le(h[0x184:0x194])
        img.parent_uuid = uuid_from_le(h[0x194:0x1A4])
    else:
        if len(h) < 0x1C8:
            img.error = "truncated VDI header"
            return
        typ = struct.unpack_from("<I", h, 0x4C)[0]
        img.virtual_size = struct.unpack_from("<Q", h, 0x170)[0]
        img.uuid = uuid_from_le(h[0x188:0x198])
        img.modify_uuid = uuid_from_le(h[0x198:0x1A8])
        img.parent_uuid = uuid_from_le(h[0x1A8:0x1B8])
        img.parent_modify_uuid = uuid_from_le(h[0x1B8:0x1C8])
    img.kind = VDI_TYPES.get(typ, f"type {typ}")


# --------------------------------------------------------------------------
# VMDK parsing
# --------------------------------------------------------------------------

EXTENT_RE = re.compile(
    r'^(RW|RDONLY|NOACCESS)\s+(\d+)\s+(\w+)(?:\s+"([^"]*)")?(?:\s+(\d+))?',
    re.IGNORECASE)


def parse_vmdk(img):
    size = getsize(img.path)
    img.file_size = size
    with open(fs(img.path), "rb") as f:
        head = f.read(512)
        if head[:4] == b"KDMV":
            capacity, _grain, doff, dsize = struct.unpack_from("<QQQQ", head, 12)
            if doff and dsize:
                # monolithicSparse / streamOptimized: descriptor is embedded.
                f.seek(doff * SECTOR)
                raw = f.read(min(dsize * SECTOR, MAX_DESCRIPTOR_FILE))
                parse_descriptor(img, raw, embedded=True)
                return
            img.role = "extent"
            img.kind = "sparse extent"
            img.virtual_size = capacity * SECTOR
            return
        if head[:4] == b"COWD":
            img.role, img.kind = "extent", "vmfsSparse extent"
            return
        if len(head) >= 8 and struct.unpack_from("<Q", head)[0] == 0xCAFEBABE:
            img.role, img.kind = "extent", "seSparse extent"
            return
        if size is not None and size <= MAX_DESCRIPTOR_FILE:
            f.seek(0)
            raw = f.read()
            if b"createType" in raw or b"Disk DescriptorFile" in raw:
                parse_descriptor(img, raw, embedded=False)
                return
    img.role = "extent"
    img.kind = "flat/raw extent (no header)"


def parse_descriptor(img, raw, embedded):
    raw = raw.split(b"\x00", 1)[0]
    m = re.search(rb'(?im)^\s*encoding\s*=\s*"([^"]+)"', raw)
    enc = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        text = raw.decode(enc, errors="replace")
    except LookupError:
        text = raw.decode("latin-1")

    base_dir = os.path.dirname(img.path)
    kv = {}
    total_sectors = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = EXTENT_RE.match(line)
        if m:
            total_sectors += int(m.group(2))
            etype = m.group(3).upper()
            fname = m.group(4)
            if embedded:
                continue        # the data lives in this same file
            if not fname or etype == "ZERO":
                continue
            p = resolve_ref(fname, base_dir)
            if not os.path.exists(fs(p)):
                alt = os.path.join(base_dir, basename_any(fname))
                if os.path.exists(fs(alt)):
                    p = alt
                else:
                    img.missing_extents.append(fname)
                    continue
            img.extents.append(p)
        elif "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip().lower()] = v.strip().strip('"')

    img.kind = kv.get("createtype", "?") + (" (embedded descriptor)" if embedded else "")
    img.virtual_size = total_sectors * SECTOR if total_sectors else None
    img.cid = norm_cid(kv.get("cid"))
    img.parent_cid = norm_cid(kv.get("parentcid"), is_parent=True)
    img.parent_hint = kv.get("parentfilenamehint") or None
    img.uuid = norm_uuid(kv.get("ddb.uuid.image"))
    img.parent_uuid = norm_uuid(kv.get("ddb.uuid.parent"))
    img.modify_uuid = norm_uuid(kv.get("ddb.uuid.modification"))
    img.parent_modify_uuid = norm_uuid(kv.get("ddb.uuid.parentmodification"))

    if not embedded:
        total = img.file_size or 0
        for e in img.data_files:
            total += getsize(e) or 0
        img.file_size = total


# --------------------------------------------------------------------------
# VM configuration files (.vbox, .vmx, .vmsd) for labels
# --------------------------------------------------------------------------

def localname(tag):
    return tag.rsplit("}", 1)[-1]


def parse_vbox(path, uuid_refs, registry):
    """uuid_refs: list of (uuid, label, set of expected path keys)
    registry: uuid -> list of (location, vbox path)"""
    root = ET.parse(fs(path)).getroot()
    machine = next((e for e in root.iter() if localname(e.tag) == "Machine"), None)
    if machine is None:
        return
    vm = machine.get("name", "?")
    vbox_dir = os.path.dirname(path)

    expected = defaultdict(set)
    for e in machine.iter():
        if localname(e.tag) == "HardDisk":
            u = norm_uuid(e.get("uuid"))
            loc = e.get("location")
            if u and loc:
                registry[u].append((loc, path))
                expected[u].add(norm_path(resolve_ref(loc, vbox_dir)))

    def collect(elem, label):
        stack = list(elem)
        while stack:
            e = stack.pop()
            t = localname(e.tag)
            if t in ("Snapshot", "Snapshots", "MediaRegistry"):
                continue
            if t == "AttachedDevice" and e.get("type") == "HardDisk":
                for im in e:
                    if localname(im.tag) == "Image":
                        u = norm_uuid(im.get("uuid"))
                        if u:
                            uuid_refs.append((u, label, expected.get(u, set())))
                continue
            stack.extend(list(e))

    collect(machine, f'VirtualBox VM "{vm}" (current state)')

    def walk_snapshots(elem):
        for e in elem:
            t = localname(e.tag)
            if t == "Snapshot":
                collect(e, f'VirtualBox VM "{vm}", snapshot "{e.get("name", "?")}"')
                walk_snapshots(e)
            elif t == "Snapshots":
                walk_snapshots(e)

    walk_snapshots(machine)


def read_kv_file(path):
    kv = {}
    with open(fs(path), "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r'^\s*([^#=\s][^=]*?)\s*=\s*"(.*)"\s*$', line)
            if m:
                kv[m.group(1).lower()] = m.group(2)
    return kv


def parse_vmx(path, path_refs):
    kv = read_kv_file(path)
    vm = kv.get("displayname") or os.path.splitext(os.path.basename(path))[0]
    d = os.path.dirname(path)
    for k, v in kv.items():
        m = re.match(r"^((?:scsi|sata|ide|nvme)\d+:\d+)\.filename$", k)
        if m and v.lower().endswith(".vmdk"):
            path_refs.append((resolve_ref(v, d), os.path.join(d, basename_any(v)),
                              f'VMware VM "{vm}" ({m.group(1)}, current state)'))


def parse_vmsd(path, path_refs):
    kv = read_kv_file(path)
    vm = os.path.splitext(os.path.basename(path))[0]
    d = os.path.dirname(path)
    for k, v in kv.items():
        m = re.match(r"^(snapshot\d+)\.disk\d+\.filename$", k)
        if m:
            snap = kv.get(f"{m.group(1)}.displayname", "?")
            path_refs.append((resolve_ref(v, d), os.path.join(d, basename_any(v)),
                              f'VMware VM "{vm}", snapshot "{snap}"'))


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def scan(folders):
    images, configs = {}, {}
    n_dirs = 0
    for root in folders:
        if not os.path.isdir(root):
            warn(f"Not a folder, skipped: {root}")
            continue
        for dirpath, _dirs, files in os.walk(root, onerror=warn):
            n_dirs += 1
            PROGRESS.update(f"{n_dirs} folders, {len(images)} image files, "
                            f"{len(configs)} VM configs - {dirpath}")
            for fn in files:
                low = fn.lower()
                full = os.path.join(dirpath, fn)
                if low.endswith((".vdi", ".vmdk")):
                    images.setdefault(norm_path(full), Image(full))
                elif low.endswith((".vbox", ".vmx", ".vmsd")):
                    configs.setdefault(norm_path(full), full)
    PROGRESS.done(f"{n_dirs} folders searched, {len(images)} image files and "
                  f"{len(configs)} VM config files found")
    return images, list(configs.values())


# --------------------------------------------------------------------------
# Linking
# --------------------------------------------------------------------------

def same_dir(a, b):
    return os.path.dirname(a.key) == os.path.dirname(b.key)


def same_drive(a, b):
    return os.path.splitdrive(a.key)[0] == os.path.splitdrive(b.key)[0]


def link_images(disks, registry):
    by_uuid, by_path, by_name, by_cid = (defaultdict(list), {},
                                          defaultdict(list), defaultdict(list))
    for d in disks:
        by_path[d.key] = d
        by_name[os.path.normcase(os.path.basename(d.path))].append(d)
        if d.uuid:
            by_uuid[d.uuid].append(d)
        if d.fmt == "VMDK" and d.cid:
            by_cid[d.cid].append(d)

    # Mark copies that share a UUID; this is common after copying images.
    for u, group in by_uuid.items():
        if len(group) > 1:
            for d in group:
                others = [o.path for o in group if o is not d]
                d.link_notes.append(f"{len(others)} other file(s) have the same UUID "
                                    f"(probably copies): " + "; ".join(others))

    for i, img in enumerate(disks, 1):
        PROGRESS.counter(i, len(disks), "images")
        if not img.wants_parent:
            continue
        cands = {}

        def add(c, pts, why):
            if c is img:
                return
            e = cands.setdefault(c.key, [c, 0, []])
            if why not in e[2]:
                e[1] += pts
                e[2].append(why)

        if img.parent_uuid:
            for c in by_uuid.get(img.parent_uuid, []):
                add(c, 100, "UUID")
        if img.parent_hint:
            hp = resolve_ref(img.parent_hint, os.path.dirname(img.path))
            c = by_path.get(norm_path(hp))
            if c:
                add(c, 30, "exact path hint")
            for c in by_name.get(os.path.normcase(basename_any(img.parent_hint)), []):
                add(c, 20, "filename hint")
        if img.parent_uuid and img.parent_uuid in registry:
            for loc, _cfg in registry[img.parent_uuid]:
                for c in by_name.get(os.path.normcase(basename_any(loc)), []):
                    add(c, 15, "filename from .vbox registry")
        if img.parent_cid and not cands:
            for c in by_cid.get(img.parent_cid, []):
                add(c, 5, "CID only")

        if not cands:
            continue

        for e in cands.values():
            c = e[0]
            if img.parent_cid and c.cid == img.parent_cid:
                e[1] += 40
                e[2].append("CID matches")
            if img.parent_modify_uuid and c.modify_uuid == img.parent_modify_uuid:
                e[1] += 10
                e[2].append("modification UUID matches")
            if same_dir(img, c):
                e[1] += 3
            elif same_drive(img, c):
                e[1] += 1

        ranked = sorted(cands.values(), key=lambda e: (-e[1], e[0].path.lower()))
        best, score, reasons = ranked[0]
        img.parent = best
        img.link_method = ", ".join(reasons)

        if "UUID" in reasons:
            img.confidence = "high"
        elif "CID matches" in reasons and "exact path hint" in reasons:
            img.confidence = "high"
        elif "CID matches" in reasons:
            img.confidence = "medium"
        else:
            img.confidence = "low"

        if len(ranked) > 1 and ranked[1][1] == score:
            img.confidence += ", ambiguous"
            img.link_notes.append("Several candidate parents scored equally; the "
                                  "choice between them is arbitrary.")
        for c, s, r in ranked[1:]:
            img.alternates.append(f"{c.path}  (score {s}: {', '.join(r)})")

        if img.parent_uuid and best.uuid and best.uuid != img.parent_uuid:
            img.link_notes.append(f"WEAK MATCH: expected parent UUID {img.parent_uuid}, "
                                  f"but the matched file has UUID {best.uuid}.")
        if img.parent_cid and best.cid and best.cid != img.parent_cid:
            img.link_notes.append(
                f"CID mismatch: this image expects parentCID={img.parent_cid}, but the "
                f"parent has CID={best.cid}. The parent may have been modified after "
                f"this image was created, which can make this image's data inconsistent.")
        if (img.parent_modify_uuid and best.modify_uuid
                and img.parent_modify_uuid != best.modify_uuid):
            img.verbose_notes.append(
                "Parent modification UUID differs from the one recorded in this image. "
                "VirtualBox does not always keep these in sync, so this is informational.")

    # Break any cycles (possible with corrupted or very odd metadata).
    for img in disks:
        seen = set()
        cur = img
        while cur.parent is not None:
            if cur.key in seen:
                cur.link_notes.append(f"Parent link to {cur.parent.path} removed "
                                      f"because it created a loop.")
                cur.parent = None
                break
            seen.add(cur.key)
            cur = cur.parent

    for img in disks:
        if img.parent is not None:
            img.parent.children.append(img)

    linked = sum(1 for d in disks if d.parent is not None)
    PROGRESS.done(f"{linked} of {len(disks)} images linked to a parent")


# --------------------------------------------------------------------------
# Dependency lists
# --------------------------------------------------------------------------

def ancestors(img):
    """Parent, grandparent, ... nearest first."""
    out, cur = [], img.parent
    while cur is not None:
        out.append(cur)
        cur = cur.parent
    return out


def descendants(img):
    """(image, generation) pairs in depth-first order, so each image is
    followed by the images that depend on it."""
    out, seen = [], {img.key}

    def walk(node, gen):
        for c in sorted(node.children, key=lambda c: c.path.lower()):
            if c.key in seen:
                continue
            seen.add(c.key)
            out.append((c, gen))
            walk(c, gen + 1)

    walk(img, 1)
    return out


def relation_name(gen, up):
    if gen == 1:
        return "parent" if up else "child"
    if gen == 2:
        return "grandparent" if up else "grandchild"
    return f"{gen} generations {'up' if up else 'down'}"


def missing_parent_text(img, unreadable_by_name, registry):
    """Describe the parent that img expects but that was not found."""
    bits = []
    if img.parent_hint:
        bits.append(f"expected file '{basename_any(img.parent_hint)}'")
    if img.parent_uuid:
        bits.append(f"UUID {img.parent_uuid}")
    if img.parent_cid and not img.parent_hint:
        bits.append(f"CID {img.parent_cid}")
    lines = [f"MISSING PARENT of {os.path.basename(img.path)}: not found in the "
             f"scanned folders ({', '.join(bits)})"]
    if img.parent_hint:
        lines.append(f"  recorded path: {img.parent_hint}")
    for loc, cfg in registry.get(img.parent_uuid, []) if img.parent_uuid else []:
        lines.append(f"  listed in {cfg} as: {loc}")
    if img.parent_hint:
        for bad in unreadable_by_name.get(
                os.path.normcase(basename_any(img.parent_hint)), []):
            lines.append(f"  a file with this name exists but could not be read: "
                         f"{bad.path} ({bad.error})")
    return lines


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def link_text(child):
    return f"matched by {child.link_method}; confidence {child.confidence}"


def describe(img):
    return (f"{img.fmt} {img.kind}, {human(img.virtual_size)} virtual size, "
            f"{human(img.file_size)} on disk")


def report_entry(img, n, verbose, unreadable_by_name, registry):
    out = []
    ind = "    "
    out.append("-" * 78)
    out.append(f"[{n}] {img.path}")
    out.append(f"{ind}Type: {describe(img)}")
    for r in sorted(set(img.vm_refs)):
        out.append(f"{ind}Used by: {r}")
    if not img.vm_refs:
        out.append(f"{ind}Used by: no VM configuration file found that uses it")
    for e in img.data_files:
        out.append(f"{ind}Its own data file: {e}")
    for m in img.missing_extents:
        out.append(f"{ind}! Its own data file is MISSING: {m}")

    # Images this one relies on.
    anc = ancestors(img)
    top = anc[-1] if anc else img
    chain_broken = top.wants_parent and top.parent is None
    out.append("")
    if not anc and not chain_broken:
        out.append(f"{ind}RELIES ON: nothing (this is a base image)")
    else:
        if not anc:
            out.append(f"{ind}RELIES ON: a parent image that was not found")
        else:
            out.append(f"{ind}RELIES ON ({len(anc)} image file(s), nearest first"
                       + (", plus a missing parent):" if chain_broken else "):"))
        child = img
        for i, a in enumerate(anc, 1):
            label = relation_name(i, up=True)
            if a is top and not chain_broken:
                label += ", base image"
            out.append(f"{ind}  {i}. {a.path}")
            out.append(f"{ind}       ({label}; {link_text(child)})")
            for e in a.data_files:
                out.append(f"{ind}       data file: {e}")
            for m in a.missing_extents:
                out.append(f"{ind}       ! data file MISSING: {m}")
            child = a
        if chain_broken:
            for line in missing_parent_text(top, unreadable_by_name, registry):
                out.append(f"{ind}  ! {line}")
            out.append(f"{ind}  ! This image cannot be used until the missing "
                       f"parent is found.")

    # Images that rely on this one.
    desc = descendants(img)
    out.append("")
    if not desc:
        out.append(f"{ind}RELIED ON BY: nothing (no image found that depends on this one)")
    else:
        direct = sum(1 for _, g in desc if g == 1)
        out.append(f"{ind}RELIED ON BY ({len(desc)} image file(s), {direct} directly; "
                   f"each indented entry depends on the entry above it):")
        for d, gen in desc:
            pad = "  " * gen
            extra = ""
            if d.confidence and not d.confidence.startswith("high"):
                extra = f"  (link confidence {d.confidence})"
            out.append(f"{ind}{pad}{d.path}  [{relation_name(gen, up=False)}]{extra}")
            if d.data_files:
                out.append(f"{ind}{pad}  + {len(d.data_files)} data file(s) of its own")

    notes = [f"! {x}" for x in img.link_notes]
    notes += [f"other candidate parent: {a}" for a in img.alternates]
    if verbose:
        notes += [f"(info) {x}" for x in img.verbose_notes]
        for label, val in (("uuid", img.uuid), ("parent uuid", img.parent_uuid),
                           ("CID", img.cid), ("parentCID", img.parent_cid),
                           ("parentFileNameHint", img.parent_hint)):
            if val:
                notes.append(f"{label}: {val}")
    if notes:
        out.append("")
        out.append(f"{ind}NOTES:")
        for x in notes:
            out.append(f"{ind}  {x}")
    out.append("")
    return out


def build_report(folders, disks, configs, orphan_extents, errors, registry, verbose):
    unreadable_by_name = defaultdict(list)
    for e in errors:
        unreadable_by_name[os.path.normcase(os.path.basename(e.path))].append(e)

    out = []

    def section(title):
        out.append("")
        out.append("=" * 78)
        out.append(title)
        out.append("=" * 78)

    bases = sum(1 for d in disks if not d.wants_parent)
    broken = sum(1 for d in disks if d.wants_parent and d.parent is None)
    out.append("VIRTUAL DISK IMAGE DEPENDENCY REPORT")
    out.append(f"Created: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out.append("Folders searched:")
    for f in folders:
        out.append(f"  {f}")
    out.append("")
    out.append(f"Disk images found: {len(disks)}")
    out.append(f"  base images (no parent needed): {bases}")
    out.append(f"  images linked to a parent:      {sum(1 for d in disks if d.parent)}")
    out.append(f"  images whose parent is missing: {broken}")
    out.append(f"VM configuration files read: {len(configs)}")
    out.append(f"Orphaned VMDK data files: {len(orphan_extents)}")
    out.append(f"Files that could not be read: {len(errors)}")
    out.append("")
    out.append("How to read each entry:")
    out.append("  RELIES ON    - files this image needs. Deleting, moving out of reach or")
    out.append("                 modifying any of them can break this image.")
    out.append("  RELIED ON BY - images that need this file. Deleting or modifying this")
    out.append("                 image can break all of them.")
    out.append("  'data file'  - a separate VMDK file (for example -flat.vmdk or")
    out.append("                 -s001.vmdk) that holds the contents of a .vmdk descriptor.")

    section(f"DISK IMAGES ({len(disks)}), sorted by path")
    for n, img in enumerate(sorted(disks, key=lambda d: d.path.lower()), 1):
        PROGRESS.counter(n, len(disks), "entries")
        out.extend(report_entry(img, n, verbose, unreadable_by_name, registry))

    if orphan_extents:
        section(f"VMDK DATA FILES NOT REFERENCED BY ANY DESCRIPTOR FOUND "
                f"({len(orphan_extents)})")
        out.append("These hold disk data, but their descriptor .vmdk file was not found.")
        for e in sorted(orphan_extents, key=lambda d: d.path.lower()):
            out.append(f"  {e.path}  [{e.kind}, {human(e.file_size)}]")

    if errors:
        section(f"FILES THAT COULD NOT BE READ ({len(errors)})")
        for e in sorted(errors, key=lambda d: d.path.lower()):
            out.append(f"  {e.path}: {e.error}")

    if WARNINGS:
        section(f"WARNINGS ({len(WARNINGS)})")
        for w in WARNINGS:
            out.append(f"  {w}")

    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="For each .vdi/.vmdk image, list the images it relies on and "
                    "the images that rely on it.")
    ap.add_argument("folders", nargs="+", help="folders to scan recursively")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                    help=f"report file to write (default: {DEFAULT_OUTPUT})")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also show UUIDs, CIDs and raw parent filename hints")
    args = ap.parse_args()
    args.folders = [os.path.abspath(f) for f in args.folders]

    # 1. Find files.
    PROGRESS.start(1, "Searching folders")
    images, configs = scan(args.folders)

    # 2. Read image headers.
    PROGRESS.start(2, "Reading disk image headers")
    for i, img in enumerate(images.values(), 1):
        PROGRESS.counter(i, len(images), "files")
        try:
            if img.fmt == "VDI":
                parse_vdi(img)
            else:
                parse_vmdk(img)
        except OSError as e:
            img.error = f"could not read: {e}"
        except (struct.error, ValueError) as e:
            img.error = f"could not parse: {e}"

    # Attach data extents to the descriptors that reference them.
    for img in images.values():
        for e in img.data_files:
            k = norm_path(e)
            if k in images:
                images[k].owner = img

    disks = [i for i in images.values() if i.role == "disk" and not i.error]
    orphan_extents = [i for i in images.values()
                      if i.role == "extent" and i.owner is None and not i.error]
    errors = [i for i in images.values() if i.error]
    PROGRESS.done(f"{len(disks)} disk images, "
                  f"{len(images) - len(disks) - len(errors)} VMDK data files, "
                  f"{len(errors)} unreadable")

    # 3. Read VM configuration files for labels and the VirtualBox media registry.
    PROGRESS.start(3, "Reading VM configuration files")
    uuid_refs, path_refs, registry = [], [], defaultdict(list)
    for i, cfg in enumerate(configs, 1):
        PROGRESS.counter(i, len(configs), "files")
        try:
            low = cfg.lower()
            if low.endswith(".vbox"):
                parse_vbox(cfg, uuid_refs, registry)
            elif low.endswith(".vmx"):
                parse_vmx(cfg, path_refs)
            else:
                parse_vmsd(cfg, path_refs)
        except (OSError, ET.ParseError) as e:
            warn(f"Could not read VM config {cfg}: {e}")
    PROGRESS.done(f"{len(configs)} files read")

    # 4. Match parents and children.
    PROGRESS.start(4, "Matching parents and children")
    link_images(disks, registry)

    by_uuid = defaultdict(list)
    by_key = {d.key: d for d in disks}
    for d in disks:
        if d.uuid:
            by_uuid[d.uuid].append(d)
    for u, label, expected in uuid_refs:
        matches = by_uuid.get(u, [])
        exact = [m for m in matches if m.key in expected]
        for m in exact or matches:
            m.vm_refs.append(label)
    for p1, p2, label in path_refs:
        d = by_key.get(norm_path(p1)) or by_key.get(norm_path(p2))
        if d:
            d.vm_refs.append(label)

    # 5. Write the report.
    PROGRESS.start(5, "Writing report")
    lines = build_report(args.folders, disks, configs, orphan_extents, errors,
                         registry, args.verbose)
    try:
        # utf-8-sig adds a byte-order mark so Windows Notepad shows non-ASCII
        # filenames correctly.
        with open(args.output, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        PROGRESS.done("failed")
        sys.stderr.write(f"Could not write {args.output}: {e}\n")
        return 1
    PROGRESS.done(f"{len(disks)} entries written")

    sys.stderr.write(f"\nReport written to {os.path.abspath(args.output)}\n")
    if WARNINGS:
        sys.stderr.write(f"{len(WARNINGS)} warning(s); see the end of the report.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
