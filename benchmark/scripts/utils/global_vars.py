"""
Global variables loader for Python scripts.
All scripts should use this module to load global variables.
"""

import os
import subprocess
from pathlib import Path
from typing import Dict

_GLOBAL_VARS_CACHE: Dict[str, str] = {}


def load_global_vars(force_reload: bool = False) -> Dict[str, str]:
    """
    Load global variables from global_vars.sh.

    Args:
        force_reload: Force reload even if cached

    Returns:
        Dictionary of environment variables
    """
    global _GLOBAL_VARS_CACHE

    if _GLOBAL_VARS_CACHE and not force_reload:
        return _GLOBAL_VARS_CACHE

    # Find global_vars.sh
    current_file = Path(__file__).resolve()
    global_vars_path = current_file.parent.parent.parent / 'configs' / 'global_vars.sh'

    if not global_vars_path.exists():
        raise FileNotFoundError(f"global_vars.sh not found at {global_vars_path}")

    # Source the file and extract environment variables
    result = subprocess.run(
        ['bash', '-c', f'source {global_vars_path} && env'],
        capture_output=True,
        text=True,
        check=True
    )

    env_vars = {}
    for line in result.stdout.split('\n'):
        if '=' in line:
            key, value = line.split('=', 1)
            # Only keep relevant variables
            if key.startswith(('DIR_', 'PYENV_', 'SCRIPT_', 'JSON_', 'TIMEOUT_', 'HF_', 'MODELSCOPE_')):
                env_vars[key] = value

    _GLOBAL_VARS_CACHE = env_vars
    return env_vars


# Convenience accessors
def get_skill_env_root() -> Path:
    """Get DIR_ENV_ROOT as Path."""
    vars = load_global_vars()
    return Path(vars['DIR_ENV_ROOT'])


def get_models_dir() -> Path:
    """Get models directory."""
    vars = load_global_vars()
    return Path(vars['DIR_MODELS'])


def get_ir_dir() -> Path:
    """Get IR directory."""
    vars = load_global_vars()
    return Path(vars['DIR_IR'])


def get_quantized_dir() -> Path:
    """Get quantized models directory."""
    vars = load_global_vars()
    return Path(vars['DIR_QUANTIZED'])


def get_logs_dir() -> Path:
    """Get logs directory."""
    vars = load_global_vars()
    return Path(vars['DIR_LOGS'])


def get_scripts_dir() -> Path:
    """Get generated scripts directory."""
    vars = load_global_vars()
    return Path(vars['DIR_SCRIPTS'])


def get_templates_root() -> Path:
    """Get templates root directory."""
    vars = load_global_vars()
    return Path(vars['DIR_TEMPLATES_ROOT'])


# Initialize environment variables in current process
def init_env():
    """Initialize environment variables in current process."""
    vars = load_global_vars()
    os.environ.update(vars)


if __name__ == '__main__':
    # Test
    vars = load_global_vars()
    print("Loaded global variables:")
    for k, v in sorted(vars.items()):
        print(f"  {k}={v}")
