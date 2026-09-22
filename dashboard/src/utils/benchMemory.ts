// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The memory arithmetic behind the memory check the run review shows: what a
// (model, precision) costs to load, how much of a given device's free memory it
// would take, and how large a context ("window") the remaining memory allows.
//
// Peak memory of a decode run is roughly
//   weights + kv_cache_per_token * window + activations + logits + overhead.
// The dialog reports the two ends the cached data can state precisely: the
// weights-only baseline (the least it can possibly need, window = 0) and, given
// a free-memory budget, the largest window whose KV cache still fits. Activation
// and framework overhead are runtime-dependent and deliberately left out rather
// than guessed.

import type {
  BenchDevice,
  BenchModelMemory,
  BenchPrecision,
  DynamicInfoData,
  StaticInfoData,
} from '../api/types'
import { friendlyGpuLabel } from './gpu'

/**
 * On-disk/loaded weight size for a precision: the authoritative file size when
 * the cache has it, else the parameter-count estimate. null when neither is
 * known (a v1 cache, or a repo published without size metadata).
 */
export function weightsBytes(
  mem: BenchModelMemory | null | undefined,
  precision: BenchPrecision,
): number | null {
  if (!mem) return null
  return mem.weights_bytes?.[precision] ?? mem.weights_bytes_est?.[precision] ?? null
}

/**
 * The least memory a (model, precision) can need: weights plus the vocab-wide
 * logits, with no KV cache (window = 0). null when the weights are unknown.
 */
export function minNeededBytes(
  mem: BenchModelMemory | null | undefined,
  precision: BenchPrecision,
): number | null {
  const weights = weightsBytes(mem, precision)
  if (weights == null) return null
  return weights + (mem?.logits_bytes ?? 0)
}

/** The model's architectural context limit (max tokens), or null if unknown. */
export function modelMaxWindow(mem: BenchModelMemory | null | undefined): number | null {
  return mem?.max_window_size ?? mem?.arch?.max_position_embeddings ?? null
}

/**
 * The context lengths the dialog offers, and the one it opens on.
 *
 * A benchmark's own default is a short prompt with 128 new tokens, so 1k is
 * both the realistic starting point and the length at which the KV cache is
 * small enough that the bar is honestly mostly weights.
 */
export const WINDOW_CHOICES = [1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072]
export const DEFAULT_WINDOW = 1024

/**
 * How a requirement lands against a budget, in the four words the preflight
 * dialogs answer in. Shared by the memory check and the disk check so the two
 * say the same thing about the same situation.
 *
 * `full` is over budget outright, `tight` fits with too little left to be
 * comfortable, `unknown` is a budget that could not be read -- never reported
 * as a problem, since nothing was measured.
 */
export type FitVerdict = 'unknown' | 'fits' | 'tight' | 'full'

/**
 * How much of a device's free memory a load may take before it counts as tight.
 *
 * The estimate leaves out activations and framework overhead (runtime-dependent
 * and deliberately not guessed), so a load that fills the last sliver of free
 * memory on paper is one that fails in practice. 85% is where the missing terms
 * stop being noise.
 */
export const MEMORY_BUDGET_RATIO = 0.85

export function memoryVerdict(needed: number | null, availBytes: number | null): FitVerdict {
  if (needed == null || availBytes == null) return 'unknown'
  if (needed > availBytes) return 'full'
  if (needed > availBytes * MEMORY_BUDGET_RATIO) return 'tight'
  return 'fits'
}

/**
 * How many sequences at once the dialog offers, and the one it opens on.
 *
 * One, because that is what the generated llm_bench command runs. The rest are
 * there to answer "and if I batched it" without a second reading of the table:
 * concurrency multiplies the KV cache and nothing else, so it moves exactly one
 * part of the bar.
 */
export const STREAM_CHOICES = [1, 2, 4, 8, 16]
export const DEFAULT_STREAMS = 1

/**
 * KV-cache bytes for `streams` sequences of `window` tokens, or null when the
 * per-token size is unknown.
 *
 * The cache is per token per sequence, so both multiply. Weights do not: one
 * copy serves every sequence, which is why the bar keeps them apart.
 */
export function kvCacheBytes(
  mem: BenchModelMemory | null | undefined,
  window: number,
  streams = 1,
): number | null {
  if (!mem?.kv_cache_bytes_per_token) return null
  return mem.kv_cache_bytes_per_token * window * streams
}

/** e.g. 4831838208 -> "4.8 GB". Sub-GB drops to MB so small models read right. */
export function formatBytesGB(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return 'N/A'
  const gb = bytes / 1e9
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  const mb = bytes / 1e6
  return `${mb.toFixed(0)} MB`
}

/**
 * e.g. 32768 -> "32K", 40960 -> "40K", 131072 -> "128K", 1536 -> "1.5K".
 *
 * Context lengths are powers of two and are named as such everywhere they are
 * published ("32K context", "128K context"), so the K here is 1024 and not
 * 1000. Dividing by 1000 turned every one of them into an off-by-one oddity --
 * 32768 printed as "33k" against a model card that calls it 32K.
 *
 * A length that is not a whole number of K keeps one decimal rather than being
 * rounded into a neighbouring power of two.
 */
export function formatTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return 'N/A'
  const trim = (value: number) =>
    Number.isInteger(value) ? `${value}` : value.toFixed(1)
  if (n >= 1024 * 1024) return `${trim(n / (1024 * 1024))}M`
  if (n >= 1024) return `${trim(n / 1024)}K`
  return `${Math.round(n)}`
}

/** One thing a device selection resolves to: a place to run, and its free memory. */
export interface DeviceTarget {
  /** Display label, e.g. "CPU", "NPU", "GPU.0 (iGPU)", "GPU.1 (dGPU)". */
  label: string
  /** Free bytes for this target now, or null when it cannot be read. */
  availBytes: number | null
  /** Where the number came from, for the "shared memory" note on an iGPU. */
  source: 'system' | 'vram'
}

// Intel iGPU is always at PCI bus 00, device 02 (e.g. 0000:00:02.0). Same test
// as SystemOverview.buildGpuDevices uses to tell integrated from discrete.
const IGPU_PCI = /(^|:)00:02\./

/**
 * A card key the monitor could name, i.e. one that is a GPU to compute with.
 *
 * monitor/metrics/gpu_info.py's card_to_gpu_label() names a DRM card by the
 * render node it is paired with -- "GPU.0", "GPU.1", matching the order OpenCL
 * and Level Zero enumerate in -- and returns the raw sysfs name ("card1") when
 * there is no render node to pair with. A card without a render node cannot run
 * anything: on a server that is the BMC's display chip, which has no business
 * being offered as a benchmark target. So the shape of the key is the filter,
 * and the naming stays where it is decided.
 */
const GPU_KEY = /^GPU\.\d+$/

function systemAvailBytes(dyn: DynamicInfoData | null | undefined): number | null {
  const gb = dyn?.memory?.available_gb
  return typeof gb === 'number' ? gb * 1024 ** 3 : null
}

/**
 * The device targets a selected `device` maps to, each with its free memory.
 *
 * CPU and NPU both run out of system RAM, so they report system free memory.
 * A GPU selection expands to one target per card that can actually compute (the
 * user wants them all shown): an iGPU shares system memory, while a discrete GPU
 * has its own VRAM (free = total - used). A machine with no GPU cards still
 * yields a single "GPU" target off system memory, so the row is never empty.
 *
 * Each target is named by the id the monitor gave the card and what kind of card
 * it is -- "GPU.1 (dGPU)". The id leads because it is the identity: it is what
 * the rest of the dashboard keys that card by, and the kind is a property of it.
 */
export function resolveDeviceTargets(
  device: BenchDevice,
  dyn: DynamicInfoData | null | undefined,
  stat: StaticInfoData | null | undefined,
): DeviceTarget[] {
  if (device === 'cpu') return [{ label: 'CPU', availBytes: systemAvailBytes(dyn), source: 'system' }]
  if (device === 'npu') return [{ label: 'NPU', availBytes: systemAvailBytes(dyn), source: 'system' }]

  // device === 'gpu': enumerate every card known to either static or dynamic info.
  const cardKeys = new Set<string>()
  Object.keys(stat?.gpu?.vram ?? {}).forEach((k) => cardKeys.add(k))
  Object.keys(stat?.gpu?.pci_addresses ?? {}).forEach((k) => cardKeys.add(k))
  Object.keys(dyn?.gpu?.vram ?? {}).forEach((k) => cardKeys.add(k))

  // Numeric ordering, so a machine with ten cards lists GPU.9 before GPU.10.
  const all = Array.from(cardKeys).sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true }),
  )
  // Display-only devices drop out here. If that leaves nothing -- an older
  // monitor, or a naming this does not recognise -- everything is kept: a table
  // with a questionable row in it is still more use than an empty one.
  const named = all.filter((key) => GPU_KEY.test(key))
  const keys = named.length ? named : all
  if (keys.length === 0) {
    // No card metadata at all: fall back to a single system-memory GPU target
    // rather than reporting nothing.
    return [{ label: 'GPU', availBytes: systemAvailBytes(dyn), source: 'system' }]
  }

  return keys.map((cardKey) => {
    const pci = (stat?.gpu?.pci_addresses?.[cardKey] ?? '').toLowerCase()
    const integrated = IGPU_PCI.test(pci)
    // friendlyGpuLabel keeps iGPU/dGPU spelled as the rest of the dashboard
    // spells it; the card's own id is what tells two of the same kind apart.
    const kind = friendlyGpuLabel(integrated ? 'integrated' : 'discrete', 'GPU')
    const label = `${cardKey} (${kind})`
    if (integrated) {
      // Shares system memory: there is no VRAM figure to read for it.
      return { label, availBytes: systemAvailBytes(dyn), source: 'system' as const }
    }
    const vram = dyn?.gpu?.vram?.[cardKey] ?? stat?.gpu?.vram?.[cardKey]
    const total = vram?.total_bytes ?? null
    const used = vram?.used_bytes ?? null
    const free = total != null && used != null ? Math.max(0, total - used) : null
    return { label, availBytes: free, source: 'vram' as const }
  })
}
