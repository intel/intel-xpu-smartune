// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

/**
 * Shown after every user-initiated restore, on each entry point that lifts a limit.
 *
 * Lifting the cap does not hand the app its memory back at once: what it gave up
 * while limited comes back as the app runs, so it can feel sluggish for a while.
 * The hint only sets that expectation -- it deliberately says nothing about *how*
 * the memory was taken away, which is not something the operator acts on.
 */
export const MEMORY_RECOVERY_HINT =
  'Its memory usage will recover gradually, so the app may respond slowly for a short while.'

/**
 * Shown in the confirmation dialog of the manual Freeze action.
 *
 * Freeze is an escalation, not a default: the ordinary limit only throttles CPU,
 * memory and disk, and an app that runs on a GPU/NPU needs very little of those per
 * unit time, so the limit may not slow it down.  The text must say what freeze costs.
 */
export const FREEZE_WHY =
  'Apps that mainly run on a GPU/NPU need very little CPU, memory or disk per unit of time, '
  + 'so the resource limit may not reduce how much of the device they use. Freezing pauses the '
  + 'whole app, which frees the device for higher-priority apps.'

export const FREEZE_RISKS: string[] = [
  'The app makes no progress while frozen. Network/RPC requests and heartbeats can time out.',
  'Everything in the same scope is frozen together (a terminal tab is one scope).',
  'On an NPU the device may take a while to free up: work already submitted keeps running '
  + '(about 20 s measured for an 8B model).',
  'Memory held on the device is not released.',
  'On a GPU the usage reading may not drop while the app is frozen, although the device is '
  + 'actually free for other apps; judge by how the higher-priority app performs.',
  'Restore lifts the limits and unfreezes the app.',
]
