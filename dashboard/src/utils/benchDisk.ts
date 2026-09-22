// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The disk arithmetic behind the Download preflight dialog: what the next fetch
// will write, and whether the volume it writes to has room for it.
//
// A download's cost is the weight files of the OpenVINO repo for each requested
// precision -- `hf download --local-dir <models>/<model>/<precision>_ov`, one
// directory per conversion, hundreds of megabytes to tens of gigabytes. The
// authoritative figure is the repo's own file sizes, which search_models.py
// caches as memory.weights_bytes; the parameter-count estimate stands in when
// the hub could not be asked. Both come through weightsBytes() in ./benchMemory,
// so the download check and the memory check cannot disagree about a size.
//
// A precision already on disk is not re-fetched at any size: `hf download` into
// a populated --local-dir re-uses what is there, so it is listed for what it
// holds and contributes nothing to what is needed.

import type { BenchModel, BenchPrecision } from '../api/types'
import { weightsBytes, type FitVerdict } from './benchMemory'

/**
 * Free space the check wants left over once the download has landed.
 *
 * Not a hard requirement -- the fetch itself only needs its own size. It is the
 * margin below which "it fits" stops being useful information: the pipeline
 * writes run outputs, logs and an HF cache onto this same volume, and a machine
 * left with a gigabyte will fail at the next thing it does instead of at the
 * download.
 */
export const DISK_HEADROOM_BYTES = 5e9

/** One model, with the precisions the download would fetch for it. */
export interface DiskPreflightItem {
  model: BenchModel
  precisions: BenchPrecision[]
}

/** One (model, precision) line of the dialog's table. */
export interface DiskRow {
  key: string
  model: string
  precision: BenchPrecision
  /** Already downloaded: the fetch re-uses it and adds nothing. */
  local: boolean
  /** What it is holding now, when it is already downloaded. */
  localBytes: number | null
  /** What fetching it would write, or null when the size is unknown. */
  needBytes: number | null
  // How many rows this model spans, set only on the first of them (0 on the
  // rest) so the model column merges its cells.
  modelRowSpan: number
}

/**
 * The download's per-precision cost, every model in one list.
 *
 * One table rather than one per model: the question is what this download costs
 * altogether, and a column of model names with the repeats merged answers it in
 * one place. A model's rows are emitted contiguously, so the first carries the
 * span and the rest collapse into it.
 *
 * Every requested precision gets a line, including the ones already on disk:
 * "this one you already have" is the answer to half the question the dialog is
 * asked, and dropping those rows would make a re-download of six precisions
 * look like an empty plan.
 */
export function buildDiskRows(items: DiskPreflightItem[]): DiskRow[] {
  const rows: DiskRow[] = []
  for (const item of items) {
    const start = rows.length
    const mem = item.model.memory
    for (const precision of item.precisions) {
      const local = !!item.model.local?.[precision]
      rows.push({
        key: `${item.model.id}-${precision}`,
        model: item.model.id,
        precision,
        local,
        localBytes: item.model.local_bytes?.[precision] ?? null,
        needBytes: local ? 0 : weightsBytes(mem, precision),
        modelRowSpan: 0,
      })
    }
    if (rows.length > start) rows[start].modelRowSpan = rows.length - start
  }
  return rows
}

/** What a set of rows adds up to, and how much of it could not be priced. */
export interface DiskTotals {
  /** Bytes the download will write, summing only the sizes that are known. */
  required: number
  /** Precisions that will be fetched but whose size is unknown. */
  unknown: number
  /** Precisions that will actually be fetched (the rest are already here). */
  fetching: number
}

export function diskTotals(rows: DiskRow[]): DiskTotals {
  let required = 0
  let unknown = 0
  let fetching = 0
  for (const row of rows) {
    if (row.local) continue
    fetching += 1
    if (row.needBytes == null) unknown += 1
    else required += row.needBytes
  }
  return { required, unknown, fetching }
}

/**
 * How a download's size lands against the free space on the target volume.
 *
 * The same four words the memory check uses (FitVerdict), so "Tight" does not
 * mean two different things in two dialogs -- only what earns it differs: disk
 * keeps an absolute margin, memory a ratio of what the device has free.
 *
 * A download whose size is partly unknown is still judged on the part that is
 * known: the total can only be larger than what was summed, so a `full` verdict
 * stands and a `fits` one is reported alongside the unknown count.
 */
export function diskVerdict(required: number, freeBytes: number | null): FitVerdict {
  if (freeBytes == null) return 'unknown'
  if (required > freeBytes) return 'full'
  if (freeBytes - required < DISK_HEADROOM_BYTES) return 'tight'
  return 'fits'
}
