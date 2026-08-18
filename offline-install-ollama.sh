#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install Ollama + qwen2.5:3b-instruct on an AIR-GAPPED system (no network).
#
# Input: a bundle from prepare-offline-ollama.sh, i.e. a tar containing
#   ollama-linux-amd64.tar.zst
#   ollama-linux-amd64.tar.zst.sha256
#   models/...            (Ollama model library, ready layout)
#
# What it does:
#   1. verifies the Ollama tarball checksum
#   2. unpacks Ollama into /usr/local (root required)
#   3. creates the 'ollama' system user and installs ollama.service
#   4. restores the model library to /usr/share/ollama/.ollama/models
#   5. starts the service and verifies the model is available
#
# Usage:  ./offline-install-ollama.sh /path/to/filestore-search-offline-llm.tar
#         (or just the tar basename if it is in the current directory)
# ---------------------------------------------------------------------------
set -euo pipefail

BUNDLE="${1:-}"
[[ -n "$BUNDLE" && -f "$BUNDLE" ]] || {
    echo "Usage: $0 /path/to/filestore-search-offline-llm.tar" >&2
    exit 1
}

command -v sha256sum >/dev/null || { echo "ERROR: sha256sum not found" >&2; exit 1; }

echo "==> Unpacking bundle"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar -xf "$BUNDLE" -C "$WORK"
[[ -f "$WORK/ollama-linux-amd64.tar.zst" ]] || {
    echo "ERROR: bundle does not contain ollama-linux-amd64.tar.zst" >&2
    exit 1
}

command -v zstd >/dev/null || {
    if [[ -f "$WORK/zstd.rpm" ]]; then
        echo "==> zstd not found; installing bundled zstd.rpm"
        if rpm -ivh --replacepkgs "$WORK/zstd.rpm" 2>/dev/null; then
            : # installed cleanly
        else
            # .rpm is built for a specific distro (e.g. RHEL 9); if rpm
            # refuses it (libgcc/glibc mismatch), fall back to extracting
            # the (static) zstd binary out of the rpm.
            echo "    rpm install failed (distro mismatch); extracting binary"
            EX="$(mktemp -d)"
            rpm2cpio "$WORK/zstd.rpm" | (cd "$EX" && cpio -idm --quiet)
            install -m 0755 "$EX/usr/bin/zstd" /usr/local/bin/zstd
            rm -rf "$EX"
            command -v zstd >/dev/null || {
                echo "ERROR: zstd binary extraction failed" >&2; exit 1
            }
        fi
    else
        echo "ERROR: zstd not found and no zstd.rpm in bundle." >&2
        echo "       Install zstd from OS media (rpm -ivh <media>/zstd-*.rpm)," >&2
        echo "       or rebuild the bundle with the zstd rpm as 4th argument of" >&2
        echo "       prepare-offline-ollama.sh." >&2
        exit 1
    fi
}

echo "==> Verifying checksum"
( cd "$WORK" && sha256sum -c ollama-linux-amd64.tar.zst.sha256 )

echo "==> Installing Ollama to /usr/local"
mkdir -p /usr/local/lib/ollama
zstd -dc "$WORK/ollama-linux-amd64.tar.zst" | tar -xpf - -C /usr/local
/usr/local/bin/ollama --version

echo "==> Creating ollama system user (if absent)"
id ollama >/dev/null 2>&1 || useradd -r -s /sbin/nologin -U -m -d /usr/share/ollama ollama

echo "==> Installing ollama.service"
# Keep in sync with ollama.service in this repo (CPU-friendly: 127.0.0.1
# bind, 4096 context, capped at ~6 cores so the search app keeps CPU).
cat > /etc/systemd/system/ollama.service <<'EOF'
[Unit]
Description=Ollama LLM server (local, CPU)
After=network.target

[Service]
User=ollama
Group=ollama
Type=simple
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_CONTEXT_LENGTH=4096
# Be a good citizen on a shared CPU box: cap at ~6 cores so search/indexing
# and the web UI still get CPU. Remove if this box is dedicated to the LLM.
CPUQuota=600%
ExecStart=/usr/local/bin/ollama serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
# Ollama keeps its model library under /usr/share/ollama/.ollama
ReadWritePaths=/usr/share/ollama
EOF

echo "==> Restoring model library to /usr/share/ollama/.ollama/models"
mkdir -p /usr/share/ollama/.ollama/models
cp -a "$WORK/models/." /usr/share/ollama/.ollama/models/
chown -R ollama:ollama /usr/share/ollama/.ollama

echo "==> Enabling + starting ollama"
systemctl daemon-reload
systemctl enable --now ollama
sleep 2

echo "==> Verifying model"
/usr/local/bin/ollama list | grep -q "qwen2.5" ||
    { echo "WARNING: qwen2.5 not listed; check 'ollama list' manually" >&2; }
/usr/local/bin/ollama list

echo
echo "Done. Ollama is serving qwen2.5:3b-instruct on 127.0.0.1:11434."
echo "filestore-search's defaults already point at it"
echo "(llm_base=http://127.0.0.1:11434/v1, llm_model=qwen2.5:3b-instruct)."
echo "Verify with:  curl -s http://127.0.0.1:11434/api/tags"
