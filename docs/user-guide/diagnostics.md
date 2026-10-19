# System diagnostics

[← Back to contents](../README.md)

> Shown only when diagnostics is enabled on the server.

**Purpose:** answer *"what happened, when, and what did SmarTune do about it?"*
Diagnostics turns raw events and logs into a timeline with severity, alerts, and
plain-language **advice**, so you can investigate a slowdown after the fact.

<!-- Screenshot: the full System diagnostics tab — summary cards on top, the
     event timeline in the middle, the event/log tables below. -->
![System diagnostics](images/diagnostics-overview.png)

## Picking the time range

Everything on the page is scoped to a time range. You can:

- Select a **boot cycle** to look at one power-on session.
- Arrive pre-scoped from [Home](home.md) or [History](history.md) when you drill
  in from there.
- **Refresh** to pull the latest events.

## Summary panels

Across the top, a set of cards summarize the selected range:

- **Selected time range overview** — the headline counts for the window.
- **Event profile** / **Event severity distribution** — how many events, split by
  severity.
- **Event counts by domain** — which subsystem (CPU, memory, disk, network,
  control, benchmark, …) generated the events.
- **Resource average utilization** — average load over the window, for context.

Cards are actionable — for example, *View alerts in the selected range* and
*View advice items for the selected range* filter the lists below.

## Event activity timeline

The **event activity timeline** plots events over time, colored by severity (see
the **severity legend**). This is the fastest way to spot a burst of activity.

<!-- Screenshot: the event activity timeline with a visible burst of events. -->
![Event timeline](images/diagnostics-timeline.png)

- **Zoom** into a span to narrow the range, and **Reset zoom** to go back.

## Events, alerts, and logs

Below the timeline are the detailed tables.

**Events / Alerts.** Search and filter the event list by **severity**,
**domain**, **kind**, and **source**. **Alerts** are the higher-severity events
derived from this stream. Select any row to open **Event Detail** with the full
context of that event.

<!-- Screenshot: the Event Detail panel open for one event. -->
![Event detail](images/diagnostics-event-detail.png)

**Logs.** Search log messages within the selected range and filter by **log
level** and **log source**.

> On SmarTune, logs stay on the single machine and are not written to journald;
> alerts are *derived from events*, so what you see here is self-contained to
> this host.

## Advice

The **Advices** view lists expert, plain-language recommendations derived from
what happened — e.g. which app to re-prioritize, or which limit is worth
revisiting. From a diagnostics context you can also take direct action, such as
**Restore all active limits for an application**.

## Export a report

Use **Diagnostics report** to **download the complete report as JSON** for the
selected range — handy for sharing an investigation or attaching to a bug.

<!-- Screenshot: the Diagnostics report export control / dialog. -->
![Diagnostics report export](images/diagnostics-report.png)

---

Next: **[History](history.md)** — longer-term trends behind these events.
