// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import type { GpuUsageDevice } from '../api/types'

// dev_type is typically "integrated"/"discrete" → iGPU/dGPU.
export function friendlyGpuLabel(devType?: string | null, fallback = 'GPU'): string {
  const t = devType?.toLowerCase()
  if (t?.startsWith('int')) return 'iGPU'
  if (t?.startsWith('dis')) return 'dGPU'
  return devType || fallback
}

// Build a pdev -> display label map from the dynamic_info device list.  The map
// is keyed by PCI address (drm-pdev), which is how per-process gpu_devices are
// keyed too, so callers can resolve a process's device to an iGPU/dGPU label.
//
// When several devices share a base label (e.g. two discrete GPUs both "dGPU")
// the PCI address is appended so they can be told apart — matching the
// "dGPU (0000:03:00.0)" convention used elsewhere in the dashboard.
export function buildGpuLabelMap(
  devices: GpuUsageDevice[] | undefined | null,
): Map<string, string> {
  const map = new Map<string, string>()
  if (!devices || devices.length === 0) return map

  const bases = devices.map((d, i) => friendlyGpuLabel(d.dev_type, d.pci_dev || `GPU ${i}`))
  const counts = new Map<string, number>()
  bases.forEach((b) => counts.set(b, (counts.get(b) ?? 0) + 1))

  devices.forEach((d, i) => {
    const base = bases[i]
    const label = (counts.get(base) ?? 0) > 1 && d.pci_dev ? `${base} (${d.pci_dev})` : base
    map.set(d.pci_dev || `GPU ${i}`, label)
  })
  return map
}
