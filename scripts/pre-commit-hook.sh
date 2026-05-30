#!/bin/bash
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
cd "$REPO_ROOT" || exit 1
if [ ! -f "$REPO_ROOT/.gitleaks.toml" ]; then echo "[!] .gitleaks.toml not found. Skipping."; exit 0; fi
if ! command -v gitleaks >/dev/null 2>&1; then echo "[!] gitleaks not installed. Skipping."; exit 0; fi
echo "[*] Scanning for secrets..."
git stash -q --keep-index --include-untracked 2>/dev/null || true
gitleaks detect --source "$REPO_ROOT" --no-git --config "$REPO_ROOT/.gitleaks.toml" -v 2>&1
R=$?
git stash pop -q 2>/dev/null || true
if [ $R -ne 0 ]; then echo ""; echo "[!] SECRET DETECTED - Commit blocked!"; echo "    To bypass: git commit --no-verify"; exit 1; fi
echo "[+] No secrets detected."
exit 0

