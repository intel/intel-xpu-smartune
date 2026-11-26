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

# Words that make a key a secret: the key must end in one, after any prefix
# (OPENAI_API_KEY and PGPASSWORD match; max_tokens and keyboard do not).
_SECRET_WORDS = (r"(?:token|secret|password|passwd|pwd|api[_-]?key|apikey|access[_-]?key"
                 r"|private[_-]?key|auth[_-]?key|credentials?|authorization|cookie)")
# The prefix is bounded to avoid quadratic backtracking on lines like "a.a.a.a...".
_KV_KEYS = rf"(?:[A-Za-z0-9_.-]{{0,48}}?{_SECRET_WORDS})"

# key=value / key: value, key optionally quoted. A quoted value is redacted to its closing
# quote (or end of line past 512 chars); a bare one to whitespace or a delimiter.
_KV_RE = re.compile(
    rf"(?i)\b({_KV_KEYS})([\"']?\s*[=:]\s*)"
    rf"(?:([\"'])(.{{0,512}}?)\3|([\"'])[^\n]*|([^\s\"',;&]+))",
)

# CLI flags: --token VALUE / --hf-token=VALUE / --password=VALUE.
_FLAG_RE = re.compile(
    rf"(?i)(--?{_KV_KEYS}[=\s]+)([^\s\"',;]+)",
)
# mysql-family clients take the password glued to -p (-pS3cret); elsewhere -p
# is usually a port, so only these commands are touched.
_MYSQL_PASSWORD_RE = re.compile(
    r"(?i)(\b(?:mysql|mysqldump|mysqladmin|mariadb|mariadb-dump)\b[^\n]*?\s-p)(?=\S)([^\s\"']+)")
# curl -u user:password / --user user:password: keep the user, drop the password.
# A numeric uid:gid (docker run -u 1000:1000, sudo -u) is not a credential.
_USERPASS_FLAG_RE = re.compile(r"(?<!\S)((?:-u|--user)\s+(?!\d+:\d+(?!\S))[^\s:]+:)(\S+)")

# Header-style values that may contain spaces and should be fully redacted.
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*)([^\n,;]+)")
_COOKIE_HEADER_RE = re.compile(r"(?i)(cookie\s*:\s*)([^\n,;]+)")

# Bare credentials recognisable by shape alone, wherever they appear.
_BARE_SECRET_RES = tuple(re.compile(pattern) for pattern in (
    r"(?i)\bhf_[A-Za-z0-9]{8,}\b",                       # Hugging Face
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",                    # GitHub
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}",            # OpenAI / Anthropic style
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",                    # Slack
    r"\bAKIA[0-9A-Z]{16}\b",                              # AWS access key id
    r"\bAIza[0-9A-Za-z_-]{35}\b",                         # Google API key
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
))
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]+)")

# Redact URL userinfo while preserving the scheme and host for diagnostics. The
# scheme is bounded like the key prefix above, against quadratic backtracking.
_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,31}://)([^/\s:@]+:[^/\s@]+)@")

# Keys whose value should be dropped wholesale when scrubbing a dict.
_SECRET_KEY_RE = re.compile(rf"^{_KV_KEYS}$", re.IGNORECASE)


def _kv_redact(match):
    key, sep, quote, _quoted, open_quote, _bare = match.groups()
    if quote:
        return f"{key}{sep}{quote}{_REDACTED}{quote}"
    if open_quote:
        return f"{key}{sep}{open_quote}{_REDACTED}"
    return f"{key}{sep}{_REDACTED}"


def scrub_text(text):
    """Redact secrets in a string. Returns the input unchanged when not a str."""
    if not isinstance(text, str) or not text:
        return text
    out = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)
    out = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}@", out)
    out = _KV_RE.sub(_kv_redact, out)
    out = _FLAG_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _MYSQL_PASSWORD_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _USERPASS_FLAG_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    for pattern in _BARE_SECRET_RES:
        out = pattern.sub(_REDACTED, out)
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
    if isinstance(value, (set, frozenset)):
        # Sets become lists, so their items are scrubbed and they serialize as JSON arrays.
        try:
            items = sorted(value)
        except TypeError:
            items = list(value)
        return [scrub(v) for v in items]
    return value
