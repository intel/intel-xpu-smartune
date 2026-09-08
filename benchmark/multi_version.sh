BASE_VENV=${PYENV_VENV_DIR}
# Side venvs live under the runtime root so they follow DIR_ENV_ROOT wherever
# SmarTune points it (see configs/global_vars.sh); scripts/utils/python_env_selector.py
# discovers them from the same location.
PYTHON_VERSION_ROOT=${DIR_ENV_ROOT:-/tmp/skill_env}/python_version
packages=("transformers==5.0.0" "transformers==5.2.0" "transformers==4.57.0" "transformers==5.5.0")
for package in "${packages[@]}"; do
    echo "Installing $package in a separate virtual environment..."
    package_name="${package//==/_}"
    side_path="${PYTHON_VERSION_ROOT}/$package_name"
    if [[ ! -d "$side_path" ]]; then
        echo "Creating virtual environment for $package at $side_path"
        $BASE_VENV/bin/python -m venv "$side_path"
        # Inherit base venv's site-packages via .pth file (--system-site-packages does not chain venvs)
        BASE_SITE=$($BASE_VENV/bin/python -c "import site; print(site.getsitepackages()[0])")
        SIDE_SITE=$($side_path/bin/python -c "import site; print(site.getsitepackages()[0])")
        echo "$BASE_SITE" > "$SIDE_SITE/base_venv.pth"
    else
        echo "Virtual environment for $package already exists at $side_path"
        continue
    fi
    $side_path/bin/pip install "$package"

    # Create wrapper scripts for CLI tools that use this venv's Python
    # This ensures `optimum-cli` uses the correct transformers version
    echo "Creating CLI wrapper scripts in $side_path/bin..."
    cat > "$side_path/bin/optimum-cli" << 'WRAPPER_EOF'
#!/bin/bash
# Wrapper to ensure optimum-cli uses this venv's Python (and transformers version)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/python" -m optimum.commands.optimum_cli "$@"
WRAPPER_EOF
    chmod +x "$side_path/bin/optimum-cli"
done
