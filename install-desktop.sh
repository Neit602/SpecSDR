#!/usr/bin/env bash
# Add PyRX to the Linux applications menu for the current user (no root needed).
# The launcher stores this folder's absolute path, so run this again if the project moves.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
data=${XDG_DATA_HOME:-$HOME/.local/share}
python=$root/.venv/bin/python

if [[ ! -x $python ]]; then
    echo "error: $python not found. Create the virtual environment first (README, section 1)." >&2
    exit 1
fi

install -Dm644 "$root/pyrx/ui/icons/pyrx.svg" "$data/icons/hicolor/scalable/apps/pyrx.svg"
install -Dm644 "$root/pyrx/ui/icons/pyrx.png" "$data/icons/hicolor/256x256/apps/pyrx.png"

# Exec needs the path quoted (the folder name may contain '#' or spaces) and escaped per the
# Desktop Entry spec: quoting first, then string-level backslashes, and '%' doubled.
exec_path=$(printf '%s' "$python" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g' -e 's/%/%%/g')
workdir=$(printf '%s' "$root" | sed -e 's/\\/\\\\/g')

install -d "$data/applications"
cat > "$data/applications/pyrx.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=PyRX
GenericName=SDR Receiver
Comment=Software-defined radio receiver with analog FPV video
Exec="$exec_path" -m pyrx
Path=$workdir
Icon=pyrx
Terminal=false
Categories=Network;HamRadio;
Keywords=SDR;radio;RTL-SDR;HackRF;FPV;
StartupWMClass=PyRX
DESKTOP

if command -v update-desktop-database >/dev/null; then
    update-desktop-database -q "$data/applications" || true
fi

echo "Installed: $data/applications/pyrx.desktop"
