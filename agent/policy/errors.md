<!-- Copyright (c) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

## Errors

On failure a `smartune` command prints `{"error": ..., "hint": ...}` and exits with a non-zero code:

| Exit code | Meaning | What to do |
|---|---|---|
| 2 | Bad arguments | Fix the command (every command accepts `--help`) and run it again |
| 3 | Cannot connect, TLS failure or timeout | Setup problem: run `smartune doctor` once, relay its first failed check and hint, and stop |
| 4 | Missing or rejected API token | Setup problem: run `smartune doctor` once, relay its first failed check and hint, and stop |
| 5 | The server reported an error | Relay the error; try a narrower query only if the hint suggests it |
| 6 | The feature is not served by this instance | Tell the user the feature (for example diagnostics or benchmark) is not installed here; `smartune caps` shows what is |
| 7 | A `smartune control` change was refused before anything was sent: the plan has blockers, or the app's state changed since the plan | Nothing was changed. Plan again (same command without `--yes`) and show the user the new plan |

- Relay the `error` and `hint` in a sentence or two and keep the user's original question in view.
- Do not troubleshoot the machine on your own after a failure (checking services, ports, files or
  permissions). `smartune doctor` is the only diagnosis to run: it checks the configuration,
  whether the server is running, TLS, the token, versions and telemetry, and names what to fix.
  Connection and token problems are fixed by whoever installed SmarTune.
- A timeout (exit code 3 with "Timed out") is not a setup problem: narrow the query as the hint
  says, without running `doctor`.
- Do not retry the same failing command in a loop.
