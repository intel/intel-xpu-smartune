# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Sensitive-value redaction.
#
# Applied at two seams: the event-write layer (emitter.py, before a summary /
# attributes reach SQLite) and the log-query read layer (log_query.py, before
# raw log lines reach the user). Business code never scrubs by hand.
#
# What must be redacted: token / Authorization / Cookie / Hugging Face & proxy
# credentials / benchmark cmdline tokens. This is a best-effort textual scrub for
# a single-host diagnostics tool -- not a cryptographic guarantee. Patterns are
# deliberately conservative (only touch clear key=value / header shapes) so we do
# not mangle ordinary log text.

import re

_REDACTED = "***REDACTED***"

# key=value / key: value where the key names a secret. Captures the key + the
# separator, replaces only the value up to a whitespace / quote / delimiter.
_KV_KEYS = r"(?:token|access[_-]?token|api[_-]?key|secret|password|passwd|pwd|authorization|cookie|hf[_-]?token|huggingface[_-]?token)"
_KV_RE = re.compile(
    rf"(?i)\b({_KV_KEYS})(\s*[=:]\s*)([\"']?)([^\s\"',;&]+)",
)

# CLI flags: --token VALUE / --hf-token=VALUE / -p VALUE-ish secret flags.
_FLAG_RE = re.compile(
    rf"(?i)(--?{_KV_KEYS}[=\s]+)([^\s\"',;]+)",
)

# Header-style values that may contain spaces and should be fully redacted.
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*)([^\n,;]+)")
_COOKIE_HEADER_RE = re.compile(r"(?i)(cookie\s*:\s*)([^\n,;]+)")

# Bare Hugging Face user tokens (hf_...) and common bearer blobs.
_HF_RE = re.compile(r"(?i)\bhf_[A-Za-z0-9]{8,}\b")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]+)")

# Keys whose value should be dropped wholesale when scrubbing a dict.
_SECRET_KEY_RE = re.compile(_KV_KEYS + r"$", re.IGNORECASE)


def scrub_text(text):
    """Redact secrets in a string. Returns the input unchanged when not a str."""
    if not isinstance(text, str) or not text:
        return text
    out = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)
    out = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_REDACTED}", out)
    out = _FLAG_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _HF_RE.sub(_REDACTED, out)
    return out


def scrub(value):
    """Recursively redact secrets in a str / dict / list. A dict value whose key
    itself names a secret is dropped wholesale; all string leaves are text-scrubbed."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                cleaned[k] = _REDACTED
            else:
                cleaned[k] = scrub(v)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value
