#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_ENV_ROOT = os.environ.get("DIR_ENV_ROOT", "/tmp/skill_env")
DEFAULT_MODEL_ROOT = os.environ.get("DIR_MODELS", str(Path(DEFAULT_ENV_ROOT) / "models"))


def safe_name(text: str) -> str:
    return text.replace("/", "__").replace(":", "_").replace(" ", "_")


def write_meta(target_dir: Path, payload: dict) -> None:
    meta_path = target_dir / "download_meta.json"
    payload["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def has_real_download_files(target_dir: Path) -> bool:
    if not target_dir.exists() or not target_dir.is_dir():
        return False

    # Success is based on real downloaded artifacts, not metadata presence.
    for p in target_dir.rglob("*"):
        if p.is_file() and p.name != "download_meta.json":
            return True
    return False


def command_path(name: str) -> str:
    candidate = Path(sys.executable).resolve().parent / name
    if candidate.exists():
        return str(candidate)

    resolved = shutil.which(name)
    if resolved:
        return resolved

    raise FileNotFoundError(f"Required command not found: {name}")


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"[INFO] Run: {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=capture_output,
        env=env,
    )


def normalize_github_target(target: str, revision: str | None) -> tuple[str, str | None, str]:
    if target.startswith("git@github.com:") or target.startswith("ssh://git@github.com/"):
        repo_path = target.split(":", 1)[1] if target.startswith("git@github.com:") else urlparse(target).path.lstrip("/")
        repo_name = Path(repo_path).stem
        return target, revision, safe_name(repo_name)

    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != "github.com":
        return target, revision, safe_name(Path(target).stem)

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return target, revision, safe_name(Path(target).stem)

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    clone_url = f"https://github.com/{owner}/{repo}.git"
    derived_revision = revision
    if len(parts) >= 4 and parts[2] == "tree" and not derived_revision:
        derived_revision = "/".join(parts[3:])
    return clone_url, derived_revision, safe_name(repo)


def normalize_network_env() -> None:
    """Fill in the HF endpoint and mirror the proxy across all four spellings.

    The inherited environment wins: the caller (SmarTune's benchmark/service/env.py, or a
    shell that sourced configs/global_vars.sh) is what knows this site's proxy,
    and overriding it here used to pin every download to one corporate proxy.
    """
    os.environ.setdefault("HF_ENDPOINT", "https://huggingface.co")

    proxy = (os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
             or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
    if proxy:
        # wget/curl/requests each read a different spelling; keep them consistent.
        for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            os.environ[key] = proxy

    print(f"[INFO] Effective HF_ENDPOINT={os.environ['HF_ENDPOINT']}")
    print(f"[INFO] Effective proxy={proxy or 'direct (no proxy configured)'}")


def resolve_huggingface_model_id(model_id: str) -> str:
    hf_cli = command_path("hf")
    result = run_cmd(
        [hf_cli, "models", "ls", "--search", model_id, "--limit", "1", "--format", "json"],
        capture_output=True,
    )

    payload = json.loads(result.stdout or "[]")
    if not payload:
        print(f"[WARN] No Hugging Face model matched search={model_id!r}, use input as-is.")
        return model_id

    first = payload[0]
    resolved_id = first.get("id") if isinstance(first, dict) else None
    if not resolved_id:
        print(f"[WARN] Unexpected Hugging Face search payload, use input as-is: {payload!r}")
        return model_id

    print(f"[INFO] Resolved Hugging Face model: {model_id} -> {resolved_id}")
    return resolved_id


def download_huggingface(model_id: str, revision: str | None, output_dir: Path, resolve: bool = True) -> Path:
    resolved_model_id = resolve_huggingface_model_id(model_id) if resolve else model_id
    target = output_dir / "huggingface" / safe_name(resolved_model_id)
    if has_real_download_files(target):
        print(f"[INFO] Skip download, model already exists: {target}")
        return target

    target.mkdir(parents=True, exist_ok=True)
    cmd = [command_path("hf"), "download", resolved_model_id, "--local-dir", str(target)]
    if revision:
        cmd.extend(["--revision", revision])
    run_cmd(cmd)
    write_meta(
        target,
        {
            "source": "huggingface",
            "model": resolved_model_id,
            "requested_model": model_id,
            "revision": revision,
            "path": str(target),
        },
    )
    return target


def download_modelscope(model_id: str, revision: str | None, output_dir: Path) -> Path:
    target = output_dir / "modelscope" / safe_name(model_id)
    if has_real_download_files(target):
        print(f"[INFO] Skip download, model already exists: {target}")
        return target

    target.mkdir(parents=True, exist_ok=True)
    cmd = [command_path("modelscope"), "download", "--model", model_id, "--local_dir", str(target)]
    if revision:
        cmd.extend(["--revision", revision])
    run_cmd(cmd)
    write_meta(
        target,
        {
            "source": "modelscope",
            "model": model_id,
            "revision": revision,
            "path": str(target),
        },
    )
    return target


def download_github(target: str, revision: str | None, output_dir: Path) -> Path:
    root = output_dir / "github"
    root.mkdir(parents=True, exist_ok=True)

    if target.endswith(".git") or target.startswith("https://github.com/") or target.startswith("git@github.com:") or target.startswith("ssh://git@github.com/"):
        clone_url, final_revision, repo_name = normalize_github_target(target, revision)
        dst = root / repo_name
        if has_real_download_files(dst):
            print(f"[INFO] Skip download, repo already exists: {dst}")
            return dst
        if dst.exists():
            shutil.rmtree(dst)
        subprocess.run(["git", "clone", clone_url, str(dst)], check=True)
        if final_revision:
            subprocess.run(["git", "-C", str(dst), "checkout", final_revision], check=True)
        write_meta(
            dst,
            {
                "source": "github",
                "url": clone_url,
                "revision": final_revision,
                "path": str(dst),
            },
        )
        return dst

    raise ValueError("For github source, --model must be a git URL, e.g. https://github.com/org/repo.git")


def default_wget_root_name(target: str) -> str:
    parsed = urlparse(target)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2:
        root_name = "_".join(path_parts[-2:])
    elif path_parts:
        root_name = path_parts[-1]
    else:
        root_name = parsed.netloc
    return safe_name(root_name)


def wget_cut_dirs(target: str) -> str:
    parsed = urlparse(target)
    path_parts = [part for part in parsed.path.split("/") if part]
    return str(max(len(path_parts) - 1, 0))


def proxy_env(proxy: str | None) -> dict[str, str]:
    """Return the environment for one wget attempt.

    ``proxy`` of None means "attempt a direct connection": the proxy variables are
    stripped rather than left at whatever the parent had, so the no-proxy attempt
    is genuinely proxy-free.
    """
    env = os.environ.copy()
    keys = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY")
    if proxy:
        for key in keys:
            env[key] = proxy
    else:
        for key in keys:
            env.pop(key, None)
    return env


def download_wget(target: str, output_dir: Path) -> Path:
    root = output_dir / "wget"
    root.mkdir(parents=True, exist_ok=True)

    dst = root / default_wget_root_name(target)
    if has_real_download_files(dst):
        print(f"[INFO] Skip download, URL contents already exist: {dst}")
        return dst

    dst.mkdir(parents=True, exist_ok=True)
    cmd = [
        command_path("wget"),
        "--recursive",
        "--no-parent",
        "--no-host-directories",
        "--cut-dirs",
        wget_cut_dirs(target),
        "--reject",
        "index.html*",
        "--directory-prefix",
        str(dst),
        target,
    ]

    # Proxies are site-specific, so they come from the environment (SmarTune
    # exports config.yaml's benchmark.http_proxy via benchmark/service/env.py) rather than
    # being hardcoded to one corporate network. With no proxy configured this is
    # a single direct attempt; with one configured we retry direct if it fails,
    # which covers a proxy that cannot reach the requested host.
    configured_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    proxies: list[str | None] = [configured_proxy, None] if configured_proxy else [None]
    last_error: subprocess.CalledProcessError | None = None
    for attempt_index, proxy in enumerate(proxies, start=1):
        try:
            print(f"[INFO] wget attempt {attempt_index}/{len(proxies)} "
                  f"using proxy={proxy or 'direct'}")
            run_cmd(cmd, env=proxy_env(proxy))
            break
        except subprocess.CalledProcessError as exc:
            last_error = exc
            print(f"[WARN] wget attempt {attempt_index}/2 failed with exit code {exc.returncode}")
            if attempt_index == len(proxies):
                raise

    if last_error and not has_real_download_files(dst):
        raise last_error

    write_meta(
        dst,
        {
            "source": "wget",
            "url": target,
            "path": str(dst),
        },
    )
    return dst


def infer_source(source: str, model: str) -> str:
    if source != "auto":
        return source

    parsed = urlparse(model)
    if parsed.scheme in {"http", "https", "ssh"}:
        if parsed.netloc.endswith("github.com") or model.endswith(".git"):
            return "github"
        return "wget"

    if model.startswith("git@github.com:"):
        return "github"

    return "huggingface"

def download_model(model: str, output_dir: Path = Path(DEFAULT_MODEL_ROOT)) -> Path:
    source = infer_source("auto", model)
    if source == "huggingface":
        return download_huggingface(model, None, output_dir, resolve=False)
    elif source == "modelscope":
        return download_modelscope(model, None, output_dir)
    elif source == "github":
        return download_github(model, None, output_dir)
    else:
        return download_wget(model, output_dir)


def have_openvino_xml(model_dir: Path) -> bool:
    have_xml=False
    for xml_file in model_dir.rglob("*.xml"):
        if xml_file.is_file():
            bin_file = xml_file.with_suffix(".bin")
            if not bin_file.exists():
                return False
        have_xml = True
    return have_xml

def main() -> int:
    parser = argparse.ArgumentParser(description="Download models from HuggingFace, ModelScope, GitHub, or generic URLs")
    parser.add_argument("--source", default="auto", choices=["auto", "huggingface", "modelscope", "github", "wget"])
    parser.add_argument("--model", required=True, help="model id for HF/MS, or git URL for GitHub")
    parser.add_argument("--revision", default=None, help="Optional revision/tag/branch/commit")
    parser.add_argument("--output-dir", default=f"{DEFAULT_ENV_ROOT}/models", help="Output directory")

    args = parser.parse_args()
    normalize_network_env()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    used_source = infer_source(args.source, args.model)
    if used_source == "huggingface":
        path = download_huggingface(args.model, args.revision, output_dir)
    elif used_source == "modelscope":
        path = download_modelscope(args.model, args.revision, output_dir)
    elif used_source == "github":
        path = download_github(args.model, args.revision, output_dir)
    else:
        path = download_wget(args.model, output_dir)

    if not has_real_download_files(path):
        print(f"[ERROR] Download completed but no real files found under: {path}")
        return 3

    print(json.dumps({"success": True, "source": used_source, "path": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
