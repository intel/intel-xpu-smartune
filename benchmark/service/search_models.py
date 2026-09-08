#!/usr/bin/env python3
"""
search_models.py - Build the model lists the Benchmark tab reads, via the `hf` CLI.

Originally the upstream project's benchmark/webui/search_models.py; carried over
here (and out of the vendor drop) when the dashboard replaced that web UI. Run as
a subprocess by models.py, and standalone/cron-able on its own.

Two-stage pipeline:

  1. List every model under the OpenVINO org (``hf models ls --author OpenVINO``)
     and record them, together with their derived source model, in
     ``models_cache_openvino.json``.

  2. Derive each OpenVINO repo's *source* model:
       - Primary: the ``base_model`` metadata on the repo (authoritative,
         returned in bulk via ``--expand baseModels``). Covers most repos.
       - Fallback: for repos without base_model metadata, strip the OpenVINO
         precision/format suffix (e.g. ``-int4-cw-ov``) and HF-search the
         remaining name, picking the best-matching public repo.
     e.g. OpenVINO/Qwen3-8B-int4-cw-ov -> Qwen/Qwen3-8B

     The deduplicated source models are written to ``models_cache.json`` —
     this is the file GET /bench/models serves. Each entry carries the OpenVINO
     repos it maps to, so the dashboard knows offline which precisions a model
     can be downloaded in, and roughly what it is.

Real-time HF queries per keystroke would be slow, so this runs in the background
(POST /bench/models/refresh, and once at service startup when the cache is stale)
and the whole cached list is served at once for the browser to filter locally.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The caches are generated data, so they go into the runtime tree rather than next
# to this file: env.build_subprocess_env() sets BENCH_MODELS_CACHE_DIR. The
# fallback only applies to a standalone run with no environment set up.
CACHE_DIR = Path(os.environ.get("BENCH_MODELS_CACHE_DIR") or HERE)
CACHE_FILE = CACHE_DIR / "models_cache.json"            # source models (the API serves this)
OV_CACHE_FILE = CACHE_DIR / "models_cache_openvino.json"  # OpenVINO repos + mapping

# Endpoint/download defaults. Anything already set in the environment wins, and
# no proxy is imposed -- proxies are site-specific and belong in config.yaml
# (benchmark.http_proxy / https_proxy), which env.py exports for us.
HF_ENV_DEFAULTS = {
    "HF_ENDPOINT": "https://huggingface.co",
    "HF_HUB_DISABLE_XET": "1",
    "HF_HUB_ENABLE_HF_TRANSFER": "0",
}

# Cache format. Bumped when the shape of models_cache.json changes; models.py
# reads both this and the original string-list format, so an existing cache keeps
# working until the next refresh replaces it.
CACHE_VERSION = 2

# Hyphen-separated tokens appended by OpenVINO conversions; stripped from the
# tail of a repo name to recover the source model name for the search fallback.
STRIP_TOKENS = {
    "ov", "fp16", "fp32", "bf16", "f16", "f32",
    "int8", "int4", "int4_sym", "int4_asym", "sym", "asym",
    "cw", "awq", "gptq", "nf4", "kvcache", "npu", "gpu", "quantized",
}

# The subset of STRIP_TOKENS that names a weight format the build stage accepts
# (benchmark/service/runner.py VALID_PRECISIONS). Recognising these in a repo name
# is what lets the dashboard offer "this model is available in int4 and fp16"
# without a single network call.
PRECISION_TOKENS = {
    "fp16": "fp16", "fp32": "fp16", "f16": "fp16", "f32": "fp16", "bf16": "fp16",
    "int8": "int8",
    "int4": "int4", "int4_sym": "int4", "int4_asym": "int4", "nf4": "int4",
}

# Metadata requested in bulk alongside the repo list. These are HF API property
# names (camelCase); the CLI's JSON output spells the same fields snake_case, so
# the two lists below do not match character for character on purpose.
EXPAND_FIELDS = ["baseModels", "pipeline_tag", "downloads", "likes", "lastModified"]

# What stage 1 must have to be useful at all. If the CLI rejects the full field
# set above (an older `hf` that does not know one of them), the listing is retried
# with just this -- degrading to the metadata-free behaviour rather than to an
# empty model list.
EXPAND_MINIMAL = ["baseModels"]


def find_hf():
    """Locate the `hf` CLI: explicit override, then the benchmark venv, then PATH.

    Returns None when there is none. That is not an error: listing models is a
    read-only HTTP query against the hub, and _http_ls does it with the standard
    library. The CLI is still preferred where it exists -- it carries the hub's
    own retry/auth behaviour -- but the model browser must not be gated on the
    benchmark environment being installed, since choosing a model is exactly what
    someone does *before* installing anything.

    The venv is checked before PATH because setup_env.sh installs huggingface-hub
    there, while a distro-packaged huggingface-cli on PATH is often a different
    (older) major version.
    """
    env_hf = os.environ.get("HF_CLI")
    if env_hf and Path(env_hf).exists():
        return env_hf
    venv_dir = os.environ.get("PYENV_VENV_DIR")
    if venv_dir:
        candidate = Path(venv_dir) / "bin" / "hf"
        if candidate.exists():
            return str(candidate)
    return shutil.which("hf")


# One page of /api/models. The hub caps a page well below the 2000 rows stage 1
# asks for, so _http_ls follows the Link header until it has enough.
HTTP_PAGE_LIMIT = 500
HTTP_TIMEOUT_SEC = 60


def _http_opener(env):
    """A urllib opener honouring the proxy settings env.py exported.

    urllib's own getproxies() reads os.environ, which usually already has them --
    but ``env`` is what every other request in this module goes through, and a
    caller that built one by hand should not silently get a direct connection.
    """
    proxies = {}
    for scheme in ("http", "https"):
        for name in (f"{scheme}_proxy", f"{scheme}_proxy".upper()):
            if env.get(name):
                proxies[scheme] = env[name]
                break
    handlers = [urllib.request.ProxyHandler(proxies)] if proxies else []
    return urllib.request.build_opener(*handlers)


def _http_ls(env, *, author=None, search=None, limit=2000, sort="downloads", expand=None):
    """List models over the hub's HTTP API. Same contract as run_ls.

    Used when there is no `hf` CLI. The rows are the API's own JSON, which is
    what the CLI prints too -- with some fields in camelCase where the CLI
    spelled them snake_case, which is why the readers here go through _pick.
    """
    base = (env.get("HF_ENDPOINT") or HF_ENV_DEFAULTS["HF_ENDPOINT"]).rstrip("/")
    params = [("limit", str(min(limit, HTTP_PAGE_LIMIT))), ("sort", sort), ("direction", "-1")]
    if author:
        params.append(("author", author))
    if search:
        params.append(("search", search))
    for field in expand or []:
        # Repeated, not comma-joined: the API reads expand as a list parameter.
        # (The CLI is the opposite -- see run_ls.)
        params.append(("expand[]", field))

    url = f"{base}/api/models?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json", "User-Agent": "smartune-benchmark"}
    token = env.get("HF_TOKEN") or env.get("HUGGINGFACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    opener = _http_opener(env)
    rows = []
    while url and len(rows) < limit:
        try:
            with opener.open(urllib.request.Request(url, headers=headers),
                             timeout=HTTP_TIMEOUT_SEC) as resp:
                page = json.loads(resp.read().decode("utf-8"))
                link = resp.headers.get("Link") or ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Partial results are still results: stage 1 asks for one big page
            # and a failure on page three should not throw away pages one and
            # two. Nothing at all is the failure the callers retry on.
            print(f"WARN: hub API failed for {author or search}: {exc}", file=sys.stderr)
            return rows or None
        if not isinstance(page, list):
            print(f"WARN: unexpected hub API payload for {author or search}", file=sys.stderr)
            return rows or None
        rows.extend(page)
        url = _next_link(link)
    return rows[:limit]


def _next_link(header):
    """The rel="next" URL out of a Link header, or None."""
    for part in header.split(","):
        chunk = part.strip()
        if 'rel="next"' in chunk and chunk.startswith("<"):
            return chunk[1:chunk.index(">")]
    return None


def hf_env():
    env = os.environ.copy()
    for k, v in HF_ENV_DEFAULTS.items():
        env.setdefault(k, v)
    return env


def run_ls(hf, env, *, author=None, search=None, limit=2000, sort="downloads", expand=None):
    """List models. ``expand`` is a list of API property names, or None.

    ``hf`` is the CLI to shell out to, or None to query the hub's HTTP API
    directly (see find_hf). Both produce the same rows.

    Returns the parsed rows, or None if the command itself failed -- callers that
    can retry with fewer fields need to tell "the CLI rejected this" apart from
    "there are no such models", which both look like an empty list.
    """
    if not hf:
        return _http_ls(env, author=author, search=search, limit=limit,
                        sort=sort, expand=expand)
    cmd = [hf, "models", "ls", "--json", "--limit", str(limit), "--sort", sort]
    if author:
        cmd += ["--author", author]
    if search:
        cmd += ["--search", search]
    if expand:
        # One comma-separated value, not a repeatable flag: `hf models ls` takes
        # "--expand" as a single string, so passing it once per field silently
        # keeps only the last one. That cost every row its base_models (forcing a
        # fallback hub search per repo), its pipeline_tag and its likes.
        cmd += ["--expand", ",".join(expand)]
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(f"WARN: timeout for {author or search}", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"WARN: hf failed for {author or search}: {out.stderr.strip()[:300]}",
              file=sys.stderr)
        return None
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        print(f"WARN: bad JSON for {author or search}", file=sys.stderr)
        return None


def base_model_of(row):
    """Return the source model id from base_model metadata, or None."""
    bm = _pick(row, "base_models", "baseModels") or {}
    models = bm.get("models") or []
    if models and models[0].get("id"):
        return models[0]["id"]
    return None


def strip_suffix(ov_id):
    """OpenVINO/Qwen3-8B-int4-cw-ov -> 'Qwen3-8B' (best-effort source name)."""
    name = ov_id.split("/", 1)[-1]
    toks = name.split("-")
    while len(toks) > 1 and toks[-1].lower() in STRIP_TOKENS:
        toks.pop()
    return "-".join(toks)


def _norm(s):
    return s.lower().replace("-", "").replace("_", "").replace(".", "")


def precision_of(ov_id):
    """Weight format an OpenVINO repo name advertises, or None.

    OpenVINO/Qwen3-8B-int4-cw-ov -> 'int4'. Read right-to-left, because a source
    name can itself contain a format-looking token (…/bert-fp16-finetuned) while
    the conversion suffix is always at the tail.
    """
    name = ov_id.split("/", 1)[-1].lower()
    for token in reversed(name.split("-")):
        fmt = PRECISION_TOKENS.get(token)
        if fmt:
            return fmt
    return None


def _pick(row, *names):
    """First present, non-empty value among ``names``. Tolerates the CLI spelling
    a field either snake_case or camelCase across versions."""
    for name in names:
        value = row.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def repo_meta(row):
    """Descriptive fields worth showing in the dashboard's model detail pane.

    Taken from the OpenVINO repo, which is what stage 1 lists -- NOT from the
    source model. Reading the source repo's own stats would mean one extra HF
    lookup per model (hundreds per refresh) for a number nobody benchmarks on, so
    the trade is deliberate: `task` is reliable (the conversion keeps the source's
    pipeline tag), `downloads`/`likes` describe the OpenVINO conversion's
    popularity rather than the original's.
    """
    return {
        "task": _pick(row, "pipeline_tag", "pipelineTag"),
        "downloads": _pick(row, "downloads") or 0,
        "likes": _pick(row, "likes") or 0,
        "last_modified": _pick(row, "last_modified", "lastModified"),
    }


def search_source(hf, env, ov_id):
    """Fallback: search the stripped name, pick the best-matching public repo."""
    cleaned = strip_suffix(ov_id)
    if not cleaned:
        return None
    rows = run_ls(hf, env, search=cleaned, limit=5) or []
    hits = [r.get("id") for r in rows if r.get("id")]
    if not hits:
        return None
    target = _norm(cleaned)
    # Prefer an exact name match on the repo's model part; else most-downloaded.
    for h in hits:
        if _norm(h.split("/", 1)[-1]) == target:
            return h
    return hits[0]


def write_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)  # atomic swap so the page never reads a half-written file


def main():
    parser = argparse.ArgumentParser(description="Cache HF model lists for the Benchmark tab")
    parser.add_argument("--author", action="append", default=[],
                        help="Org/author to list (repeatable). Default: OpenVINO")
    parser.add_argument("--no-source-map", action="store_true",
                        help="Skip source-model derivation (OpenVINO names only)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel search workers for the fallback (default 8)")
    args = parser.parse_args()

    authors = (args.author
               or [a.strip() for a in os.environ.get("AUTHORS", "").split(",") if a.strip()]
               or ["OpenVINO"])

    hf = find_hf()
    env = hf_env()
    if not hf:
        print("no `hf` CLI; listing over the hub API instead", file=sys.stderr)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    # ---- stage 1: list OpenVINO repos (with base_model + display metadata) ----
    rows = []
    seen = set()
    for author in authors:
        got = run_ls(hf, env, author=author, expand=EXPAND_FIELDS)
        if got is None:
            # Most likely an `hf` that does not know one of the expanded fields.
            # Losing the metadata costs a nicer detail pane; losing the listing
            # costs the whole feature, so drop back to the fields we must have.
            print(f"author={author}: retrying without the optional expand fields",
                  file=sys.stderr)
            got = run_ls(hf, env, author=author, expand=EXPAND_MINIMAL) or []
        print(f"author={author}: {len(got)} models", file=sys.stderr)
        for r in got:
            mid = r.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                rows.append(r)
    # Keep download-desc order for the dropdown; --sort already did this per author.

    # ---- stage 2: derive source models ----
    ov_models = []          # [{id, source, method, precision, ...meta}]
    need_search = []        # rows lacking base_model metadata
    for r in rows:
        src = None if args.no_source_map else base_model_of(r)
        entry = {"id": r["id"], "source": src, "method": "base_model" if src else None,
                 "precision": precision_of(r["id"]), **repo_meta(r)}
        if not src:
            need_search.append(r)
        ov_models.append(entry)

    if need_search and not args.no_source_map:
        print(f"searching source for {len(need_search)} repos without base_model…",
              file=sys.stderr)
        index = {m["id"]: m for m in ov_models}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            results = pool.map(lambda r: (r["id"], search_source(hf, env, r["id"])),
                               need_search)
            for ov_id, src in results:
                if src:
                    index[ov_id]["source"] = src
                    index[ov_id]["method"] = "search"

    write_json(OV_CACHE_FILE, {
        "updated_at": now, "authors": authors,
        "count": len(ov_models), "models": ov_models,
    })
    print(f"wrote {len(ov_models)} OpenVINO repos -> {OV_CACHE_FILE}", file=sys.stderr)

    # ---- source-model list for the UI (deduped, first-seen order = popularity) ----
    # One entry per source model, folding in every OpenVINO repo that maps to it.
    # The variants are what tell the dashboard which precisions are downloadable
    # without asking HuggingFace again -- the whole point of caching this shape
    # rather than a list of names.
    sources = []
    by_source = {}
    for m in ov_models:
        source = m.get("source")
        if not source:
            continue
        entry = by_source.get(source)
        if entry is None:
            entry = {
                "id": source,
                "task": m.get("task"),
                "downloads": 0,
                "likes": 0,
                "last_modified": m.get("last_modified"),
                "variants": [],
            }
            by_source[source] = entry
            sources.append(entry)
        entry["variants"].append({"repo": m["id"], "precision": m.get("precision")})
        # A source model has several conversions; describe it by the liveliest of
        # them rather than by whichever happened to be listed first.
        entry["downloads"] = max(entry["downloads"], m.get("downloads") or 0)
        entry["likes"] = max(entry["likes"], m.get("likes") or 0)
        if entry["task"] is None:
            entry["task"] = m.get("task")

    write_json(CACHE_FILE, {
        "version": CACHE_VERSION,
        "updated_at": now, "authors": authors,
        "count": len(sources), "models": sources,
    })
    print(f"wrote {len(sources)} source models -> {CACHE_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
