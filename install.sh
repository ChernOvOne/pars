#!/usr/bin/env bash
# wlfinder one-line installer.
#
#   curl -LsSf https://raw.githubusercontent.com/ChernOvOne/pars/main/install.sh | bash
#
# Installs `uv` if missing, then installs the `wlfinder` CLI as a uv tool.
set -euo pipefail

REPO="${WLFINDER_REPO:-https://github.com/ChernOvOne/pars}"
BRANCH="${WLFINDER_BRANCH:-main}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; }

if ! command -v uv >/dev/null 2>&1; then
    say "uv not found — installing it"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
    err "uv install failed — add \$HOME/.local/bin to PATH and re-run"
    exit 1
fi

say "installing wlfinder from ${REPO}@${BRANCH}"
uv tool install --force "git+${REPO}@${BRANCH}"

say "done — 'wlfinder' is installed"
if ! command -v wlfinder >/dev/null 2>&1; then
    cat <<'EOF'

Note: add uv's tool bin directory to your PATH, e.g.:

    export PATH="$HOME/.local/bin:$PATH"

then run:  wlfinder --help
EOF
else
    say "next: wlfinder init   (then edit config.yaml and .env)"
fi
