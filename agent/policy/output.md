<!-- Copyright (c) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

## Reading and reporting output

- Run the command and wait for it to finish. Each command prints exactly one JSON document.
- **Only report what the output contains.** If a field is missing or `null`, say it is unavailable.
  Never guess a number, and never invent a cause the evidence does not show.
- **Quote the evidence** behind a conclusion: event type, time, application and figures, with units.
- **Truncation.** If the output has a `_truncated` field, the result was cut to fit the size limit.
  Narrow the query (shorter `--since`, smaller `-n`, `--grep`, `--sections`, filters) and run it
  again before drawing conclusions.
- **Times** are the server's local time with its UTC offset (for example `2026-10-29 23:14:05 +0800`),
  whatever form the API used. Mention the offset when the user may be in another time zone.
- **Quiet mode.** `"quiet_mode": true` means a benchmark run has paused collection; the figures are
  stale or missing. Say so rather than reporting them as the current state.
- What the metrics mean, their units and normal ranges: see `agent/content/metrics.md`. What to
  check next for a symptom, with reference thresholds: `agent/content/methodology.md`. How common
  workloads look in the figures: `agent/content/workloads.md`.
- Answer in the user's language, with the key figures and units. Do not paste raw JSON unless asked.
