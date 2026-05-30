#!/bin/bash
set -e
DIR=$(git rev-parse --show-toplevel 2>/dev/null)
cd "$DIR" || exit 1
ln -sf "../../scripts/pre-commit-hook.sh" ".git/hooks/pre-commit"
chmod +x ".git/hooks/pre-commit"
echo "Hook installed. Testing..."
bash scripts/pre-commit-hook.sh
exit 0
