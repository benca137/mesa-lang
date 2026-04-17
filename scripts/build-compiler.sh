#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Build a native Mesa compiler binary with PyInstaller.

Usage:
  ./scripts/build-compiler.sh [--install] [--python /path/to/python3.14] [--install-dir DIR] [--name NAME]

Options:
  --install            Symlink the built binary into the install dir.
  --python PATH        Python interpreter to use for the build.
  --install-dir DIR    Where to install the `mesa` command. Default: ~/.local/bin
  --name NAME          Output binary name. Default: mesa
  --no-bootstrap       Do not auto-install PyInstaller into the local build venv.
  -h, --help           Show this help.

Environment overrides:
  MESA_PYTHON
  MESA_INSTALL_DIR
  MESA_BINARY_NAME

Examples:
  ./scripts/build-compiler.sh
  ./scripts/build-compiler.sh --install
  MESA_PYTHON=python3.14 ./scripts/build-compiler.sh --install
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="$ROOT/.build/compiler-binary"
VENV_DIR="$BUILD_ROOT/venv"
WORK_DIR="$BUILD_ROOT/pyinstaller-work"
SPEC_DIR="$BUILD_ROOT/pyinstaller-spec"
CONFIG_DIR="$BUILD_ROOT/pyinstaller-config"
DIST_DIR="$ROOT/bin"

INSTALL=0
BOOTSTRAP=1
PYTHON_BIN="${MESA_PYTHON:-}"
INSTALL_DIR="${MESA_INSTALL_DIR:-$HOME/.local/bin}"
BINARY_NAME="${MESA_BINARY_NAME:-mesa}"

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

has_python_310_plus() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

pick_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    has_python_310_plus "$PYTHON_BIN" || die "Python interpreter '$PYTHON_BIN' must be version 3.10+"
    printf '%s\n' "$PYTHON_BIN"
    return
  fi

  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    if has_python_310_plus "$candidate"; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  die "could not find a Python 3.10+ interpreter. Set MESA_PYTHON or pass --python."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install)
      INSTALL=1
      ;;
    --python)
      shift
      [[ $# -gt 0 ]] || die "--python requires a value"
      PYTHON_BIN="$1"
      ;;
    --install-dir)
      shift
      [[ $# -gt 0 ]] || die "--install-dir requires a value"
      INSTALL_DIR="$1"
      ;;
    --name)
      shift
      [[ $# -gt 0 ]] || die "--name requires a value"
      BINARY_NAME="$1"
      ;;
    --no-bootstrap)
      BOOTSTRAP=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

PYTHON_BIN="$(pick_python)"

mkdir -p "$BUILD_ROOT" "$DIST_DIR"
mkdir -p "$CONFIG_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  printf 'creating build venv with %s\n' "$PYTHON_BIN" >&2
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"

if ! "$VENV_PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
  if [[ "$BOOTSTRAP" -ne 1 ]]; then
    die "PyInstaller is not installed in $VENV_DIR. Re-run without --no-bootstrap or install it manually."
  fi
  printf 'installing PyInstaller into %s\n' "$VENV_DIR" >&2
  "$VENV_PYTHON" -m pip install --upgrade pip >/dev/null
  "$VENV_PYTHON" -m pip install pyinstaller
fi

rm -rf "$WORK_DIR" "$SPEC_DIR"
mkdir -p "$WORK_DIR" "$SPEC_DIR"

printf 'building %s/bin/%s\n' "$ROOT" "$BINARY_NAME" >&2
PYINSTALLER_CONFIG_DIR="$CONFIG_DIR" \
"$VENV_PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name "$BINARY_NAME" \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR" \
  --specpath "$SPEC_DIR" \
  --paths "$ROOT" \
  --add-data "$ROOT/std:std" \
  --collect-submodules src \
  "$ROOT/mesa.py"

BUILT_BIN="$DIST_DIR/$BINARY_NAME"
[[ -x "$BUILT_BIN" ]] || die "build finished but $BUILT_BIN was not created"

printf 'built %s\n' "$BUILT_BIN" >&2

if [[ "$INSTALL" -eq 1 ]]; then
  mkdir -p "$INSTALL_DIR"
  ln -sf "$BUILT_BIN" "$INSTALL_DIR/$BINARY_NAME"
  printf 'installed %s -> %s\n' "$INSTALL_DIR/$BINARY_NAME" "$BUILT_BIN" >&2
  case ":$PATH:" in
    *":$INSTALL_DIR:"*)
      ;;
    *)
      printf 'hint: add %s to PATH to call `%s` directly\n' "$INSTALL_DIR" "$BINARY_NAME" >&2
      ;;
  esac
fi
