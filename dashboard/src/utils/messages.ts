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
