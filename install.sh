#!/usr/bin/env bash
# Installs igterm for the current user on Fedora (no root needed except for optional dnf hints).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/igterm"
BIN="$HOME/.local/bin"

# Prefer a Python that instagrapi's dependencies (pydantic etc.) are known to ship wheels for.
PY=""
for cand in python3.13 python3.12 python3.14 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python 3 not found.  Run: sudo dnf install python3" >&2
    exit 1
fi
echo "Using $($PY --version) at $(command -v $PY)"

if ! "$PY" -c "import curses, venv, ensurepip" 2>/dev/null; then
    echo "Your Python lacks curses/venv.  Run: sudo dnf install python3 python3-libs" >&2
    exit 1
fi

mkdir -p "$PREFIX" "$BIN"
"$PY" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/python" -m pip install --upgrade pip >/dev/null
echo "Installing instagrapi (needs internet)..."
if ! "$PREFIX/venv/bin/python" -m pip install --upgrade instagrapi; then
    cat >&2 <<EOF

pip could not install instagrapi.  If the error mentions pydantic-core / building wheels,
your default Python is probably too new.  Install an older one and re-run:
    sudo dnf install python3.13
    ./install.sh
EOF
    exit 1
fi

install -m 0644 "$HERE/igterm.py" "$PREFIX/igterm.py"
cat > "$BIN/igterm" <<EOF
#!/bin/sh
exec "$PREFIX/venv/bin/python" -I "$PREFIX/igterm.py" "\$@"
EOF
chmod 0755 "$BIN/igterm"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "NOTE: $BIN is not in your PATH.  Add this to ~/.bashrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
echo
echo "Done.  Try the offline demo first:  igterm --demo"
echo "Then log in for real:               igterm"
echo "Optional helpers for /open and /copy:  sudo dnf install xdg-utils wl-clipboard"
