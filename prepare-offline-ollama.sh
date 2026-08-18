#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Prepare an air-gapped Ollama install bundle (run on a CONNECTED machine).
#
# Produces <out>/filestore-search-offline-llm.tar containing:
#   - the Ollama linux amd64 release tarball (.tar.zst) + SHA-256
#   - the model library (qwen2.5:3b-instruct) in Ollama's on-disk layout,
#     i.e. it can be unpacked onto the target as-is
#
# Then move that single tar to the offline system and run:
#   offline-install-ollama.sh /path/to/filestore-search-offline-llm.tar
#
# Requires on this machine: curl, tar, zstd (or an already-downloaded
# ollama tarball), and a running Ollama with the model pulled
# (or it will be pulled with `ollama pull` here).
#
# Usage:
#   prepare-offline-ollama.sh [output-dir] [ollama-version] [model-tag] [zstd-rpm]
#   defaults: output-dir=., version=v0.32.14, model=qwen2.5:3b-instruct
#
# The optional [zstd-rpm] is the path to a zstd .rpm (e.g. from
# `dnf download zstd`); it gets packed into the bundle so an air-gapped
# RHEL target can install it with rpm -ivh when zstd is missing.
# ---------------------------------------------------------------------------
set -euo pipefail

OUT="${1:-$(pwd)}"
VER="${2:-v0.32.14}"
MODEL="${3:-qwen2.5:3b-instruct}"
ZSTD_RPM="${4:-}"
# Ollama stores a model's manifest as .../library/<repo>/<tag> where the tag
# is the part after the ':' (e.g. qwen2.5:3b-instruct -> tag "3b-instruct").
MODEL_REPO="${MODEL%:*}"
MODEL_TAG="${MODEL#*:}"
[[ "$MODEL_TAG" == "$MODEL" ]] && MODEL_TAG="latest"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

echo "==> Ollama version:  $VER   (model: $MODEL)"
echo "==> Working dir:     $WORK"

# 1) Ollama release tarball --------------------------------------------------
TARBALL="ollama-linux-amd64.tar.zst"
TURL="https://github.com/ollama/ollama/releases/download/${VER}/${TARBALL}"
if [[ -f "$OUT/$TARBALL" ]]; then
    echo "==> Reusing $OUT/$TARBALL"
    cp "$OUT/$TARBALL" "$WORK/$TARBALL"
else
    echo "==> Downloading $TURL"
    curl -fL --retry 3 -o "$WORK/$TARBALL" "$TURL"
fi
( cd "$WORK" && sha256sum "$TARBALL" > "$TARBALL.sha256" )
cp "$WORK/$TARBALL.sha256" "$OUT/"

# 2) Model library -----------------------------------------------------------
MODEL_ROOT="/usr/share/ollama/.ollama/models"   # Ollama default (systemd, root home)
[[ -d "$MODEL_ROOT" ]] || MODEL_ROOT="$HOME/.ollama/models"

model_name_path="manifests/registry.ollama.ai/library/${MODEL_REPO}/${MODEL_TAG}"
if [[ ! -f "$MODEL_ROOT/$model_name_path" ]]; then
    echo "==> No local Ollama store with $MODEL found; pulling it here"
    command -v ollama >/dev/null || {
        echo "ERROR: no Ollama installation and no model store found." >&2
        echo "       Run this on a machine where 'ollama pull $MODEL' works." >&2
        exit 1
    }
    ollama pull "$MODEL"
    [[ -f "$MODEL_ROOT/$model_name_path" ]] || {
        echo "ERROR: manifest for $MODEL still not found under $MODEL_ROOT" >&2
        exit 1
    }
fi

echo "==> Collecting model files (only the blobs this tag references)"
model_dir="$WORK/models"
python3 - "$MODEL_ROOT/$model_name_path" "$MODEL_ROOT/blobs" \
          "$model_dir" "$model_name_path" <<'PY'
import json, os, shutil, sys
manifest_path, blob_root, out, manifest_rel = sys.argv[1:5]
m = json.load(open(manifest_path))
digests = [m["config"]["digest"]] + [l["digest"] for l in m["layers"]]
os.makedirs(os.path.join(out, "blobs"), exist_ok=True)
for d in digests:
    # manifest digests are "sha256:<hex>"; Ollama's on-disk names use "-"
    src = os.path.join(blob_root, d.replace(":", "-", 1))
    if not os.path.isfile(src):
        sys.exit(f"ERROR: blob {d} not found in {blob_root}")
    shutil.copy2(src, os.path.join(out, "blobs", os.path.basename(src)))
manifest_dest = os.path.join(out, manifest_rel)
os.makedirs(os.path.dirname(manifest_dest), exist_ok=True)
shutil.copy2(manifest_path, manifest_dest)
print(f"    {len(digests)} files (config + {len(digests)-1} layers)")
PY

# 3) Bundle ------------------------------------------------------------------
BUNDLE="$OUT/filestore-search-offline-llm.tar"
echo "==> Packing $BUNDLE"
ITEMS=("$TARBALL" "$TARBALL.sha256" models)
if [[ -n "$ZSTD_RPM" && -f "$ZSTD_RPM" ]]; then
    echo "==> Including $(basename "$ZSTD_RPM")"
    cp "$ZSTD_RPM" "$WORK/zstd.rpm"
    ITEMS+=(zstd.rpm)
fi
tar -C "$WORK" -cf "$BUNDLE" "${ITEMS[@]}"
echo
echo "Done."
echo "  bundle:   $BUNDLE  ($(du -h "$BUNDLE" | cut -f1))"
echo "  checksum: $(sha256sum "$BUNDLE" | cut -d' ' -f1)"
echo
echo "Copy this tar (plus this directory's offline-install-ollama.sh) to the"
echo "offline system and run:"
echo "  ./offline-install-ollama.sh $(basename "$BUNDLE")"
