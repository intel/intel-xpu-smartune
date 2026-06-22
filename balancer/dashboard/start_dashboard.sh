#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "================================================"
echo "  Intel XPU SmarTune Dashboard"
echo "================================================"
echo ""

# ---------------------------------------------------------------------------
# Helper: install Node.js 20 LTS automatically based on the host OS.
# Tries (in order):
#   1. NodeSource binary repo  (Ubuntu / Debian)
#   2. NodeSource binary repo  (RHEL / CentOS / Fedora)
#   3. Homebrew                (macOS)
#   4. nvm                     (any POSIX shell)
#   5. Prints manual steps and exits
# ---------------------------------------------------------------------------
install_node() {
  echo "[INFO] Node.js 20+ not found. Attempting automatic installation..."

  OS="$(uname -s)"

  # ── Ubuntu / Debian ──────────────────────────────────────────────────────
  if [ "$OS" = "Linux" ] && command -v apt-get &>/dev/null; then
    if ! command -v curl &>/dev/null && ! command -v wget &>/dev/null; then
      echo "[INFO] Installing curl first..."
      sudo apt-get update -qq
      sudo apt-get install -y curl
    fi
    echo "[INFO] Installing Node.js 20 LTS via NodeSource..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
    echo "[INFO] Node.js installed successfully."
    return 0
  fi

  # ── RHEL / CentOS / Fedora ───────────────────────────────────────────────
  if [ "$OS" = "Linux" ] && command -v dnf &>/dev/null; then
    echo "[INFO] Installing Node.js 20 LTS via NodeSource (dnf)..."
    curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
    sudo dnf install -y nodejs
    echo "[INFO] Node.js installed successfully."
    return 0
  fi

  # ── macOS (Homebrew) ─────────────────────────────────────────────────────
  if [ "$OS" = "Darwin" ] && command -v brew &>/dev/null; then
    echo "[INFO] Installing Node.js via Homebrew..."
    brew install node@20
    echo "[INFO] Node.js installed successfully."
    return 0
  fi

  # ── nvm (any system where nvm is already set up) ─────────────────────────
  # shellcheck disable=SC1090
  NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [ -s "$NVM_DIR/nvm.sh" ]; then
    echo "[INFO] Loading nvm and installing Node.js 20 LTS..."
    # shellcheck source=/dev/null
    source "$NVM_DIR/nvm.sh"
    nvm install 20
    nvm use 20
    echo "[INFO] Node.js installed via nvm."
    return 0
  fi

  # ── Nothing worked → print manual instructions ───────────────────────────
  echo ""
  echo "================================================================"
  echo "  [ERROR] Could not install Node.js automatically."
  echo "  Vite 7 requires Node.js 20.19+ or 22.12+."
  echo "  Please install Node.js 20+ manually using ONE of the methods"
  echo "  below, then re-run this script."
  echo "================================================================"
  echo ""
  echo "  Option A – NodeSource (Ubuntu / Debian):"
  echo "    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
  echo "    sudo apt-get install -y nodejs"
  echo ""
  echo "  Option B – nvm (any Linux / macOS):"
  echo "    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash"
  echo "    source ~/.bashrc          # or ~/.zshrc / ~/.profile"
  echo "    nvm install 20"
  echo "    nvm use 20"
  echo ""
  echo "  Option C – Snap (Ubuntu):"
  echo "    sudo snap install node --classic --channel=20"
  echo ""
  echo "  Option D – Homebrew (macOS):"
  echo "    brew install node@20"
  echo ""
  echo "  After installation verify with:  node --version  (should be ≥ 20.19)"
  echo ""
  exit 1
}

# ---------------------------------------------------------------------------
# Ensure Node.js ≥ 20.19 is present; auto-install/upgrade if not.
# Vite 7 requires Node.js 20.19+ or 22.12+ (crypto.hash was added in 20.19).
# ---------------------------------------------------------------------------
if ! command -v node &>/dev/null; then
  install_node
  # Re-check after attempted install
  if ! command -v node &>/dev/null; then
    echo "[ERROR] Node.js still not found after install attempt. Please install manually."
    exit 1
  fi
fi

NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
NODE_MINOR=$(node -e "console.log(process.versions.node.split('.')[1])")

# Vite 7 requires Node.js >=20.19.0 or >=22.12.0
MEETS_REQUIREMENT=false
if [ "$NODE_MAJOR" -ge 23 ]; then
  MEETS_REQUIREMENT=true
elif [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -ge 12 ]; then
  MEETS_REQUIREMENT=true
elif [ "$NODE_MAJOR" -eq 20 ] && [ "$NODE_MINOR" -ge 19 ]; then
  MEETS_REQUIREMENT=true
fi

if [ "$MEETS_REQUIREMENT" = "false" ]; then
  echo "[WARN] Node.js $(node --version) detected, but Vite 7 requires Node.js 20.19+ or 22.12+."
  echo "       Attempting to upgrade Node.js..."
  install_node
  # Re-evaluate after upgrade attempt
  NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])" 2>/dev/null || echo "0")
  NODE_MINOR=$(node -e "console.log(process.versions.node.split('.')[1])" 2>/dev/null || echo "0")
  MEETS_REQUIREMENT=false
  if [ "$NODE_MAJOR" -ge 23 ]; then
    MEETS_REQUIREMENT=true
  elif [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -ge 12 ]; then
    MEETS_REQUIREMENT=true
  elif [ "$NODE_MAJOR" -eq 20 ] && [ "$NODE_MINOR" -ge 19 ]; then
    MEETS_REQUIREMENT=true
  fi
  if [ "$MEETS_REQUIREMENT" = "false" ]; then
    echo "[ERROR] Node.js $(node --version) still does not meet the requirement (20.19+ or 22.12+)."
    echo "        Please upgrade manually:  nvm install 20.19 && nvm use 20.19"
    exit 1
  fi
fi

echo "[INFO] Node.js $(node --version) detected. ✓"

# Ensure npm is present (it ships with Node.js but double-check)
if ! command -v npm &>/dev/null; then
  echo "[ERROR] npm is not available. It normally ships with Node.js."
  echo "        Try: sudo apt-get install -y npm   or reinstall Node.js."
  exit 1
fi

# ---------------------------------------------------------------------------
# Install npm dependencies (reads dashboard/package.json)
# ---------------------------------------------------------------------------
echo "[INFO] Installing npm dependencies from package.json..."
npm install
echo "[INFO] Dependencies installed. ✓"
echo ""

# ---------------------------------------------------------------------------
# Start the Vite dev server
# ---------------------------------------------------------------------------
echo "[INFO] Starting development server → http://localhost:39527"
echo "[INFO] API proxy           → https://127.0.0.1:9001 (self-signed cert bypassed)"
echo ""
echo "Press Ctrl+C to stop."
echo ""

npm run dev
