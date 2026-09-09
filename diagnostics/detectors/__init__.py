# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Detectors: read log/metric sources and turn recognized abnormalities into
# structured operational events. Detection (judgement) is deliberately separate
# from sources (collection): sources adapt storage into records, detectors
# decide what is an event.

from diagnostics.detectors.rules import run_once, start_detector_loop

__all__ = ["run_once", "start_detector_loop"]
