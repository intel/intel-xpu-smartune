# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(slots=True)
class MetricSample:
    name: str
    ok: bool
    metrics: Dict[str, Any]
    error: Optional[str] = None


def ok(name: str, **metrics: Any) -> MetricSample:
    return MetricSample(name=name, ok=True, metrics=dict(metrics), error=None)


def err(name: str, message: str, **metrics: Any) -> MetricSample:
    return MetricSample(name=name, ok=False, metrics=dict(metrics), error=message)
