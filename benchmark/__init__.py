# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Package marker only -- nothing is exported from here on purpose.
#
# This directory is a read-only vendor drop of the upstream model-benchmark
# project (shell pipeline + Python generators that convert, quantize and
# benchmark models on Intel XPUs), with exactly two SmarTune-owned additions:
# this file and ``service/``. Importing SmarTune's integration layer must not
# drag in any of the vendored code, so the import surface stays in
# :mod:`benchmark.service`.
#
# When re-syncing from upstream: copy, never mirror-with-delete, or this file
# and ``service/`` go with it.
