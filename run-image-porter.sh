#!/usr/bin/env bash
# Launches the Air-Gapped Image Porter on macOS / Linux with no terminal needed.
# Make executable once:  chmod +x run-image-porter.sh
# Then double-click (or run ./run-image-porter.sh).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer pythonw if present (rare on mac/linux); otherwise python3.
if command -v pythonw >/dev/null 2>&1; then
    exec pythonw "$DIR/airgap_porter.py"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 "$DIR/airgap_porter.py"
else
    exec python "$DIR/airgap_porter.py"
fi
