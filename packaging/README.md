# Debian Package Build

`deb/build_deb.sh` builds a Debian package for Intel XPU SmarTune. Run the
script from the repository root or from any directory inside the repository.

## Requirements

The build host needs:

- `dpkg-deb`
- `git`
- Python 3 with `pip`
- Node.js 20.19 or newer and `npm` when the dashboard is built

The script downloads the Python runtime dependencies listed in
`requirements.txt` as wheels, so the build host needs network access to the
configured Python package index. The wheels are bundled into the `.deb` for
offline installation on the target system.

## Usage

```text
packaging/deb/build_deb.sh [VERSION] [OPTIONS]
```

`VERSION` is optional and defaults to `1.5.0`.

Options:

- `--skip-ui`: reuse `dashboard/dist` and skip the dashboard build when
  `dashboard/dist/index.html` exists. If that file is missing, the dashboard is
  built anyway.
- `--full`: build the full `smartune` package, including the balancer and
  monitor.
- `--all`: alias for `--full`.
- `-h`, `--help`: show the command-line help.

The default build creates the monitor-only package:

```bash
packaging/deb/build_deb.sh
```

Build a specific version:

```bash
packaging/deb/build_deb.sh 1.5.1
```

Reuse an existing dashboard build:

```bash
packaging/deb/build_deb.sh 1.5.1 --skip-ui
```

Build the full package with the balancer:

```bash
packaging/deb/build_deb.sh 1.5.1 --full
```

The script runs `npm ci && npm run build` by default, stages the selected
application files, downloads Python wheels, and creates the package. Staging
files are removed after a successful build.

## Output

Packages are written to the repository's `build/` directory:

- `build/smartune-monitor_<VERSION>_amd64.deb` for the default build
- `build/smartune_<VERSION>_amd64.deb` for `--full` or `--all`

## Installation

Install the generated package with `apt`, which resolves its Debian
dependencies:

```bash
sudo apt install ./build/smartune-monitor_1.5.1_amd64.deb
```

For a full build:

```bash
sudo apt install ./build/smartune_1.5.1_amd64.deb
```

The package installs the application under `/opt/intel/smartune`. Launch it
from the application menu or desktop icon. The service is started on demand
and the dashboard is opened at `https://localhost:9001`.

The full package additionally needs the system packages declared by its
control file, including the BCC/eBPF bindings and matching kernel headers.
`cpupower` is recommended for CPU frequency and governor control, but it is
not a hard installation dependency.

## Help

To display the script's built-in usage information:

```bash
packaging/deb/build_deb.sh --help
```
