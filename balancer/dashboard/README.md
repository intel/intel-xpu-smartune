# Intel XPU SmarTune Dashboard

A Grafana-inspired dark-theme web UI for the Intel Multi-Task Resource Balancer.
Built with **Vite + React 18 + TypeScript + Ant Design v5**.

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **Node.js** | ≥ 20.19 (LTS 20 recommended) | [Install instructions below](#1-install-nodejs) |
| **npm** | ships with Node.js | — |
| **Backend service** | running on `https://127.0.0.1:9001` | See root `README.md` |

---

## Quick Start

```bash
cd dashboard
./start_dashboard.sh        # auto-installs Node.js if missing, then starts the dev server
```

Open **http://localhost:39527** in your browser.

> The script forwards all `/api/*` requests to `https://127.0.0.1:9001` through the Vite proxy,
> so the backend's self-signed TLS certificate does **not** cause browser warnings.

---

## Step-by-Step Setup

### 1. Install Node.js

Choose the method that matches your system.

#### Ubuntu / Debian (recommended – NodeSource binary repo)

```bash
# Install curl if not present
sudo apt-get update && sudo apt-get install -y curl

# Add NodeSource repo and install Node.js 20 LTS
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Verify
node --version    # v20.x.x
npm  --version    # 10.x.x
```

#### Any Linux / macOS – nvm (Node Version Manager)

```bash
# Install nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash

# Reload your shell (pick whichever applies)
source ~/.bashrc      # bash
source ~/.zshrc       # zsh
source ~/.profile     # other

# Install and activate Node.js 20 LTS
nvm install 20
nvm use 20
nvm alias default 20

# Verify
node --version    # v20.x.x
```

#### Ubuntu – Snap

```bash
sudo snap install node --classic --channel=20
node --version    # v20.x.x
```

#### macOS – Homebrew

```bash
brew install node
node --version    # v20.x.x
```

---

### 2. Install npm dependencies

The dependencies are listed in `dashboard/package.json`.

```bash
cd dashboard
npm install
```

This downloads all packages into `dashboard/node_modules/` (takes ~30 s on first run).

---

### 3. Start the development server

```bash
npm run dev
```

Output:

```
  VITE v7.x  ready in xxx ms

  ➜  Local:   http://localhost:39527/
  ➜  Network: use --host to expose
```

Open **http://localhost:39527** in your browser.

---

### 4. (Optional) Build for production

```bash
npm run build        # outputs static files to dashboard/dist/
npm run preview      # serve the production build locally
```

---

## UI Tabs

| Tab | Description |
|---|---|
| **System Overview** | Live CPU, Memory, Disk I/O, Network panels; iGPU / dGPU / NPU placeholder panels |
| **App Resources** | Per-application resource consumption (CPU %, Memory MB, I/O rate) |
| **Process Resources** | Per-process details: PID, name, CPU avg %, RSS, I/O read rate |
| **Pressure** | System / Disk I/O / Network I/O pressure gauges with **LOW / MEDIUM / HIGH / CRITICAL** badges |
| **App Management** | Add apps to control list, set priority, limit/restore resources, keep-alive, delete |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[ERROR] Node.js is not installed` | Follow [Step 1](#1-install-nodejs) above |
| `npm install` fails with `EACCES` | Run `sudo chown -R $USER ~/.npm` then retry |
| Dashboard shows "Connection Error" on all panels | Make sure `BalanceService.py` is running on port 9001 |
| Port 39527 already in use | Edit `vite.config.ts` → `server.port` and change to another port |
| `node: /lib/x86_64-linux-gnu/libc.so.6: version GLIBC_2.28 not found` | Your glibc is too old for the latest Node.js binary; use `nvm install 20` to get a compatible build |
| `Vite requires Node.js version 20.19+ or 22.12+` | Your Node.js is too old; upgrade with `nvm install 20 && nvm use 20` or `sudo snap install node --classic --channel=20` |
