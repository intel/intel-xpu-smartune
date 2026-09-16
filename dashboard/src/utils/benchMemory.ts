// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The memory arithmetic behind the Run preflight dialog: what a
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
 * The largest window (in tokens) whose KV cache still fits in `availBytes`,
 * after paying for weights and logits. Clamped at zero (a config that does not
 * even fit its weights supports no window). null when the weights or the
 * per-token KV size are unknown, so the caller can say "unknown" rather than 0.
 */
export function maxWindowForBudget(
  availBytes: number | null,
  mem: BenchModelMemory | null | undefined,
  precision: BenchPrecision,
): number | null {
  if (availBytes == null || !mem?.kv_cache_bytes_per_token) return null
  const weights = weightsBytes(mem, precision)
  if (weights == null) return null
  const forKv = availBytes - weights - (mem.logits_bytes ?? 0)
  if (forKv <= 0) return 0
  return Math.floor(forKv / mem.kv_cache_bytes_per_token)
}

/** e.g. 4831838208 -> "4.8 GB". Sub-GB drops to MB so small models read right. */
export function formatBytesGB(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return 'N/A'
  const gb = bytes / 1e9
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  const mb = bytes / 1e6
  return `${mb.toFixed(0)} MB`
}

/** e.g. 90112 -> "90k", 1200000 -> "1.2M". */
export function formatTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return 'N/A'
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1000) return `${Math.round(n / 1000)}k`
  return `${Math.round(n)}`
}

/** One thing a device selection resolves to: a place to run, and its free memory. */
export interface DeviceTarget {
  /** Display label, e.g. "CPU", "NPU", "iGPU", "dGPU (card1)". */
  label: string
  /** Free bytes for this target now, or null when it cannot be read. */
  availBytes: number | null
  /** Where the number came from, for the "shared memory" note on an iGPU. */
  source: 'system' | 'vram'
}

// Intel iGPU is always at PCI bus 00, device 02 (e.g. 0000:00:02.0). Same test
// as SystemOverview.buildGpuDevices uses to tell integrated from discrete.
const IGPU_PCI = /(^|:)00:02\./

function systemAvailBytes(dyn: DynamicInfoData | null | undefined): number | null {
  const gb = dyn?.memory?.available_gb
  return typeof gb === 'number' ? gb * 1024 ** 3 : null
}

/**
 * The device targets a selected `device` maps to, each with its free memory.
 *
 * CPU and NPU both run out of system RAM, so they report system free memory.
 * A GPU selection expands to one target per card the machine has (the user
 * wants them all shown): an iGPU shares system memory, while a discrete GPU has
 * its own VRAM (free = total - used). A machine with no GPU cards still yields a
 * single "GPU" target off system memory, so the row is never empty.
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

  const keys = Array.from(cardKeys).sort()
  if (keys.length === 0) {
    // No card metadata at all: fall back to a single system-memory GPU target
    // rather than reporting nothing.
    return [{ label: 'GPU', availBytes: systemAvailBytes(dyn), source: 'system' }]
  }

  return keys.map((cardKey) => {
    const pci = (stat?.gpu?.pci_addresses?.[cardKey] ?? '').toLowerCase()
    if (IGPU_PCI.test(pci)) {
      return { label: 'iGPU', availBytes: systemAvailBytes(dyn), source: 'system' as const }
    }
    const vram = dyn?.gpu?.vram?.[cardKey] ?? stat?.gpu?.vram?.[cardKey]
    const total = vram?.total_bytes ?? null
    const used = vram?.used_bytes ?? null
    const free = total != null && used != null ? Math.max(0, total - used) : null
    // friendlyGpuLabel keeps the naming consistent with the rest of the dashboard;
    // the cardKey disambiguates when there is more than one discrete GPU.
    const base = friendlyGpuLabel('discrete', 'dGPU')
    return { label: `${base} (${cardKey})`, availBytes: free, source: 'vram' as const }
  })
}
