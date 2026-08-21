"""Index the file store into SQLite.

Walks the data directory, classifies each file, extracts metadata where
possible, and writes to a FTS5-indexed SQLite database.

Categories:
  linux-rpm, linux-deb, linux-source, linux-installer,
  windows-msi, windows-exe, windows-patch, windows-driver, windows-other,
  firmware, driver, iso, generic

Each file also gets a deliverable priority (0-3) that the search layer
uses to rank actual software / patches / firmware above the docs, check
sums, repo metadata and other housekeeping files that live next to them:
  3  installable deliverable: rpm/deb/msi/msu/exe/cpl/sh installer/
     extension, ISO install media, BIOS/firmware bundles, drivers
  2  source / archive that may contain software (tar, zip, jar, ...)
  1  repo metadata / manifests (repomd, release, Packages, control, ...)
  0  documentation / text / other (readme, .txt, .log, ...)

The database schema is versioned; `reindex` does a full rebuild, `refresh`
does an incremental upsert/delete of changed files.
"""
import os
import re
import stat
import time
import json
import sqlite3

from config import load_config

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  platform TEXT NOT NULL,
  arch TEXT NOT NULL DEFAULT 'unknown',
  file_type TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  vendor TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  mtime REAL NOT NULL DEFAULT 0,
  indexed_at REAL NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 3,
  sha256 TEXT NOT NULL DEFAULT '',
  md5 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_platform ON files(platform);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  path,
  name,
  version,
  description,
  vendor,
  content='',
  tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

# ---------------------------------------------------------------------------
# name classification
# ---------------------------------------------------------------------------

# common vendor / product hints in filenames
VENDOR_HINTS = {
    "intel": "Intel", "amd": "AMD", "nvidia": "NVIDIA", "nvida": "NVIDIA",
    "dell": "Dell", "hp": "HP", "hpe": "HPE", "lenovo": "Lenovo",
    "supermicro": "Supermicro", "supermicro": "Supermicro", "thinkpad": "Lenovo",
    "cisco": "Cisco", "juniper": "Juniper", "aruba": "Aruba",
    "vmware": "VMware", "redhat": "Red Hat", "rhel": "Red Hat", "rhba": "Red Hat",
    "suse": "SUSE", "opensuse": "openSUSE", "canonical": "Canonical",
    "ubuntu": "Ubuntu", "debian": "Debian", "microsoft": "Microsoft",
    "winserver": "Microsoft", "esxi": "VMware", "proxmox": "Proxmox",
    "brocade": "Brocade", "netapp": "NetApp", "emc": "Dell EMC",
    "hgst": "HGST", "seagate": "Seagate", "wd": "Western Digital",
    "sandisk": "SanDisk", "kingston": "Kingston", "crucial": "Crucial",
    "samsung": "Samsung", "lsi": "LSI", "microchip": "Microchip",
    "realtek": "Realtek", "broadcom": "Broadcom", "qlogic": "QLogic",
    "hewlett": "HP",
}

# word patterns that strongly suggest firmware (used when no .rom/.bin matched)
FIRMWARE_NAME_PAT = re.compile(
    r"(firmware|bios|bmc|ipmi|microcode|management\s*engine|nvme\s*firmware|u?fw)", re.I)
DRIVER_NAME_PAT = re.compile(
    r"\b(drivers?|inf|update\s*(package|utility))\b", re.I)
VENDOR_DIR_HINTS = re.compile(r"\b(dell|hp|lenovo|supermicro)\b", re.I)


def _norm(s):
    return re.sub(r"[\s\.\-_]+", " ", s).strip().lower()


ISO_PLATFORMS = [
    (re.compile(r"(rhel|centos|fedora)", re.I), "rhel"),
    (re.compile(r"\bubuntu\b", re.I), "ubuntu"),
    (re.compile(r"\bdebian\b", re.I), "debian"),
    (re.compile(r"(opensuse|sles|\bsuse\b)", re.I), "sles"),
    (re.compile(r"(windows|win10|win11|win7|win8|win\s*server|\bserver\b)", re.I), "windows"),
    (re.compile(r"\besxi\b", re.I), "esxi"),
    (re.compile(r"\bproxmox\b", re.I), "proxmox"),
    (re.compile(r"\btrueimage\b", re.I), "trueimage"),
    (re.compile(r"\blinux\b", re.I), "linux"),
]


def platform_from_name(filename, relpath):
    for pat, plat in ISO_PLATFORMS:
        if pat.search(filename) or pat.search(relpath):
            return plat
    return "unknown"


def classify(filename, relpath):
    """Return (category, platform, arch) based on name + path hints.

    Order matters:
      1. explicit extensions (rpm/deb/iso/msi/firmware bins)
      2. .exe (almost always a Windows tool, even with vendor name)
      3. firmware by name (bios/ipmi/firmware/...) for zip/tar bundles
      4. windows bundles by name/path
      5. driver bundles, then directory hints
    """
    lower = filename.lower()
    base = os.path.basename(lower)
    rl = relpath.lower()

    m = re.search(r"\.([a-z0-9]+)$", base)
    ext = m.group(1) if m else ""

    # install / boot media (also lives under a top-level isos/ dir)
    if ext == "iso" or re.search(r"(^|/)isos?/", rl):
        return ("iso", platform_from_name(filename, rl), "unknown")

    # explicit firmware extensions
    if ext in ("rom", "bin", "img", "fd", "efi", "fw", "fwu", "cap", "pac",
               "vib", "vmdk", "swu", "cpl", "hpm", "fls", "fwp", "mrb",
               "mib", "mfd", "mfm", "uimage", "dtb"):
        return ("firmware", "firmware", "unknown")

    # linux packages
    if base.endswith(".rpm"):
        arch = "unknown"
        am = re.search(r"\.([a-z0-9_]+)\.rpm$", base)
        if am and am.group(1) not in ("src",):
            arch = am.group(1)
        plat = "rhel" if ("rhel" in rl or "redhat" in rl or "centos" in rl or
                          "rhel" in base or "rhba" in base) else \
              "sles" if ("suse" in rl or "sles" in base) else \
              "opensuse" if "opensuse" in rl else "linux"
        return ("linux-rpm", plat, arch)
    if base.endswith(".deb"):
        am = re.search(r"_([a-z0-9_]+)\.deb$", base) or re.search(r"\.([a-z0-9_]+)\.deb$", base)
        arch = am.group(1) if am else "unknown"
        plat = "ubuntu" if ("ubuntu" in rl or "canonical" in rl) else \
               "debian" if ("debian" in rl or "pool" in rl) else "linux"
        return ("linux-deb", plat, arch)
    if base.endswith(".srpm"):
        return ("linux-source", "linux", "src")
    if base.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        return ("linux-source", "linux", "unknown")

    # windows packages / executables
    if base.endswith(".msi"):
        return ("windows-msi", "windows", "unknown")
    if base.endswith((".msu", ".msp")):
        # Windows Update / patch bundles
        return ("windows-patch", "windows", "unknown")
    if base.endswith(".exe"):
        cat = "windows-driver" if DRIVER_NAME_PAT.search(filename) else "windows-exe"
        return (cat, "windows", "unknown")
    if base.endswith(".sh"):
        # shell installers / extensions (e.g. 'install-linux.sh',
        # Symantec LinuxInstaller-style install scripts)
        return ("linux-installer", "linux", "unknown")

    # firmware by name (zip/tar/loose bundles from BIOS/IDRAK/IPMI releases)
    if FIRMWARE_NAME_PAT.search(filename):
        return ("firmware", "firmware", "unknown")

    # windows bundles by name or path
    if ext in ("zip", "cab", "7z", "rar", "iso") and (
            "windows" in rl or re.search(r"\bwin(10|11|dows)?\b", base) or
            re.search(r"\bserver\s*\d{4}\b", base)):
        return ("windows-other", "windows", "unknown")

    # driver bundles for hardware vendors
    if DRIVER_NAME_PAT.search(filename) and ext in ("zip", "tar", "gz", "tgz", "iso", "cab"):
        return ("driver", "unknown", "unknown")

    # extension-less files that are still deliverables: vendor installers
    # and patch binaries commonly ship with no extension
    # ('SymantecLinuxInstaller', 'vnc72we_clhxe_nt_setup', 'setup')
    if not ext and re.search(
            r"(installer|install|setup|setupx?|patch|update|unattend|silent)",
            base):
        plat = "windows" if re.search(
            r"(win\d?|nt[_ ]?setup|windows)", base) or "windows" in rl \
            else "linux" if "linux" in rl else "unknown"
        return ("linux-installer" if plat == "linux" else "windows-exe",
                plat, "unknown")

    # fallback by directory hints
    if any(k in rl for k in ("windows", "msi")):
        return ("windows-other", "windows", "unknown")
    if "firmware" in rl:
        return ("firmware", "firmware", "unknown")
    if "driver" in rl:
        return ("driver", "unknown", "unknown")
    if "linux" in rl or "rpm" in rl or "deb" in rl:
        return ("generic", "linux", "unknown")
    return ("generic", "unknown", "unknown")


# ---------------------------------------------------------------------------
# deliverable priority
# ---------------------------------------------------------------------------
# The store holds, next to every real artifact, a pile of files that are
# *about* software but are not themselves installable: checksum sidecars,
# repo metadata, readme/docs, manifests. The FTS layer ranks by text
# relevance, so a "how do I install X" or "X readme" note can outrank the
# actual .rpm/.msu. `priority_for` gives each file a 0-3 weight the search
# layer folds into the ranking so installables always beat their paperwork.

# priority 3: installable deliverables
_DELIVERABLE_CATS = {
    "linux-rpm", "linux-deb", "linux-installer", "windows-msi",
    "windows-exe", "windows-patch", "windows-driver", "firmware",
    "driver", "iso",
}
# priority 3 extension override (covers files the category heuristic missed,
# e.g. a .msu that fell into 'generic' because its name had no vendor hint)
_DELIVERABLE_EXT = {
    "rpm", "deb", "msi", "msu", "msp", "exe", "sh", "cpl", "iso",
}
# priority 2: archives that plausibly hold software
_ARCHIVE_EXT = {
    "zip", "tar", "gz", "tgz", "xz", "bz2", "7z", "rar", "jar", "whl",
}
# priority 1: repo metadata / manifests / signatures (not installable, but
# often what a user actually wants when looking up a package). Note:
# .xml files are excluded from the index entirely (config.ignored_suffixes).
_METADATA_EXT = {
    "repomd", "release", "gpg", "asc", "sig", "json", "ya", "idx",
    "listindex", "index", "control",
}
# priority 0: documentation and other non-software. A .txt is never
# installable even when a directory heuristic misclassifies it (e.g. a
# readme inside a drivers/ folder).
_DOC_EXT = {"txt", "log", "readme", "rst", "pdf", "html", "csv",
            "md", "markdown"}
# known manifest basenames that are metadata, not installables
# (repomd.xml itself is excluded from the index via ignored_suffixes)
_MANIFEST_BASENAMES = {
    "release", "packages", "repodata", "packages.gz",
    "inrelease", "control", "control.index", "packagelist", "filelists",
}


def priority_for(category, file_type, name):
    """Deliverable priority (0-3) for the search ranking.

    3 installable (rpm/deb/msi/msu/exe/sh/iso/firmware/driver)
    2 archive (zip/tar/... that may contain software)
    1 repo metadata / manifest
    0 docs / text / other

    Extension and basename decide first, because the category heuristic is
    name/path-driven and can mislabel paperwork as a deliverable.
    """
    ext = (file_type or "").lower()
    base = os.path.basename(name).lower()
    if base in _MANIFEST_BASENAMES:
        return 1
    if ext in _DOC_EXT:
        return 0
    if category in _DELIVERABLE_CATS or ext in _DELIVERABLE_EXT:
        return 3
    if ext in _METADATA_EXT:
        return 1
    if ext in _ARCHIVE_EXT:
        return 2
    # unknown: treat as a possible deliverable (be lenient)
    return 2


# ---------------------------------------------------------------------------
# metadata extraction from names
# ---------------------------------------------------------------------------

VERSION_PAT = re.compile(
    r"v?(\d+(?:\.\d+)+[a-z0-9]*(?:~[a-z0-9]+)?|(\d{8})|(\d{4}\.\d{2})|(\d{6,}))")

ARCH_SUFFIX_PAT = re.compile(
    r"\.(x86_64|aarch64|ppc64le?|s390x?|i[35]86|noarch|all|src|amd64|arm64)$")
DEB_ARCH_SUFFIX_PAT = re.compile(r"_(amd64|arm64|i386|armhf|all)$")
WIN_SUFFIX_PAT = re.compile(
    r"[-_]win(?:10|11|64|32|dows)?(?:[-_](?:x64|x86|arm64))?(?:[-_][a-z0-9]+)*$")


def _looks_like_version(s):
    return bool(re.search(r"\d", s)) and not re.fullmatch(r"20\d{2}", s)


def _version_from_prefix(pre):
    """Given the name without its trailing arch/win suffix, pull the version
    out of the hyphen/underscore-separated segments (rpm/deb style).

    The version is the first segment that looks like a dotted version
    (rpm 'kernel-5.14.0-362.8.1...' -> '5.14.0', deb 'name_1.2-0amd64' ->
    '1.2'); bare-number segments (product model numbers like 'ilo-5') are
    only used when nothing better is found.
    """
    segs = [s for s in re.split(r"[-_]", pre) if s]
    if len(segs) < 2:
        return ""
    for s in segs:
        if re.fullmatch(r"[vV]?\d+(?:\.\d+)+[a-z0-9]*", s):
            return s.lstrip("vV")
    for s in segs:
        if re.fullmatch(r"[vV]?\d+[a-z0-9]*", s) and not re.fullmatch(r"20\d{2}", s):
            return s.lstrip("vV")
    return ""


def extract_version(filename):
    base = os.path.basename(filename)
    b = re.sub(r"(\.[a-z0-9]+)+$", "", base, flags=re.I)

    # rpm: name-version-release.arch
    m = ARCH_SUFFIX_PAT.search(b)
    if m:
        v = _version_from_prefix(b[:m.start()])
        if v:
            return v
    # deb: name_version-epocharch_arch
    m = DEB_ARCH_SUFFIX_PAT.search(b)
    if m:
        v = _version_from_prefix(b[:m.start()])
        if v:
            return v
    # windows: ...-win-x64 / ...-win64 / ...-win10-win11
    m = WIN_SUFFIX_PAT.search(b)
    if m:
        v = _version_from_prefix(b[:m.start()])
        if v:
            return v

    for pat in (
        r"[xX](\d+(?:\.\d+)+[a-z0-9]*)",           # Dell-style X4.4.4
        r"[vV](\d+(?:\.\d+)+[a-z0-9]*)",          # v21.7.4.1041 / V8.6.1
        r"(?<!\d)(\d+(?:\.\d+){1,}[a-z0-9]*)",    # 5.14.0 / 4.4.4
        r"(?<!\d)(\d{8})(?!\d)",                   # 20230912
        r"(?<!\d)(\d{4}\.\d{1,2})(?!\d)",         # 2023.04
    ):
        m = re.search(pat, b)
        if m:
            return m.group(1)
    return ""


def extract_vendor(filename, relpath):
    for hint, vendor in VENDOR_HINTS.items():
        if hint.lower() in filename.lower() or hint.lower() in relpath.lower():
            return vendor
    return ""


def extract_description(filename, relpath, category):
    """Produce a human-friendly description from the filename.

    Vendor naming is noisy; we just strip the extension chain and split on
    hyphens/underscores so the words are FTS-indexable and readable.
    Dots are kept (versions like X4.4.4 stay intact).
    """
    b = os.path.basename(filename)
    text = re.sub(r"(\.[a-z0-9]+)+$", "", b, flags=re.I)
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)   # ThinkPad -> Think Pad
    text = re.sub(r"\s+", " ", text).strip()
    return text
