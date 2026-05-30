#!/bin/bash
# Install Gitleaks pre-commit hook for this repository
# Run from repo root: bash scripts/install-hooks.sh

set -e
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo '.')"

echo "📦 Installing Gitleaks pre-commit hook..."
HOOK_PATH=".git/hooks/pre-commit"
SCRIPT_PATH="scripts/pre-commit-hook.sh"

if [ ! -f "$SCRIPT_PATH" ]; then
    echo "❌ $SCRIPT_PATH not found. Run from repo root."
    exit 1
fi

ln -sf "../../$SCRIPT_PATH" "$HOOK_PATH"
chmod +x "$HOOK_PATH"

echo "✅ Pre-commit hook installed: $HOOK_PATH → $SCRIPT_PATH"
echo ""
echo "🔍 Testing hook..."
bash "$HOOK_PATH"
