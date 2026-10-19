# Common workflows

[← Back to contents](../README.md)

A few end-to-end tasks that tie the tabs together. Each assumes SmarTune is
installed and you're [signed in](getting-started.md).

## Workflow 1 — Protect an important app under load

*Goal: keep an inference service responsive even when the box gets busy.*

1. Go to **[Applications → Processes](applications.md#processes)** and find the
   service (search by name or PID).
2. Use **Add to balancer** and complete the **Add Application wizard** so
   SmarTune can match its processes.
3. Open the **[Control center](control-center.md)**, find the app, and set its
   **Priority** to **Critical**.
4. Enable **keep-alive** for it so it survives memory pressure.
5. Watch the **[Home](home.md)** Pressure Overview under load — when pressure rises,
   SmarTune limits *lower-priority* apps first and protects this one.
6. Confirm what happened in **[System diagnostics](diagnostics.md)**: the event
   timeline shows which apps were limited and why.

## Workflow 2 — Cap a disk-hungry app

*Goal: stop a backup/indexing job from starving everything else on disk.*

1. Add the job to the balancer (Workflow 1, steps 1–2) if it isn't already.
2. In the **[Control center](control-center.md)**, open the app's limits and set
   a **Disk I/O Limit** (read/write throughput and/or IOPS).
3. Observe the **Disk I/O Pressure** bar on [Home](home.md) drop as the cap
   takes effect.
4. When the job is done, use **Restore Resources** (or **Allow auto-limiting
   again**) to release the cap.

## Workflow 3 — Benchmark a model on this hardware

*Goal: decide which model/precision runs best here.*

1. Open **[Models](models.md)** and **download** the model(s) you want to test.
2. Check the **memory fit** and **disk preflight** hints.
3. **Run** a benchmark; wait for setup + run to finish.
4. Read the **results** KPIs, then open **compare** to put runs side by side.
5. Export the winner via **[Diagnostics → report](diagnostics.md#export-a-report)**
   if you want a permanent record.

## Workflow 4 — Investigate a slowdown after the fact

*Goal: understand a lag spike that already happened.*

1. Open **[History](history.md)**, find the time window where pressure spiked.
2. Drill into **[System diagnostics](diagnostics.md)** for that range (or select
   the matching **boot cycle**).
3. Use the **event timeline** and filters (severity / domain) to see what fired.
4. Open individual **Event Detail** rows and read the **Advices** for
   recommended actions.
5. Apply any fix back in the **[Control center](control-center.md)** or
   **[Settings](settings.md)**.

---

If something isn't working as described, see
**[Troubleshooting & FAQ](troubleshooting.md)**.
