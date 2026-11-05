#!/usr/bin/env bash
# Git pre-commit hook: run the famboard test suite; block the commit on failure.
# Install:  ln -sf ../../scripts/pre-commit.sh .git/hooks/pre-commit
cd "$(git rev-parse --show-toplevel)" || exit 1
echo "famboard: esecuzione test prima del commit..."
if ! .venv/bin/pytest -q; then
    echo "Commit annullato: ci sono test che falliscono." >&2
    exit 1
fi
