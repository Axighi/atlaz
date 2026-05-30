#!/usr/bin/env bash
# ============================================================================
# sync-upstream.sh — Merge upstream NousResearch/hermes-agent into atlaz
#
# Automatically resolves rename-only conflicts (hermes ↔ atlaz) and flags
# real conflicts for manual review.
#
# Usage:
#   bash scripts/sync-upstream.sh          # merge upstream/main
#   bash scripts/sync-upstream.sh --push   # merge + push to origin
#   bash scripts/sync-upstream.sh --abort  # abort a merge in progress
# ============================================================================
set -euo pipefail

UPSTREAM_REMOTE="upstream"
UPSTREAM_BRANCH="main"
MERGE_MSG="chore: merge upstream/hermes-agent $(date +%Y-%m-%d)"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ------------------------------------------------------------------
# Color helpers
# ------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[sync]${NC} $1"; }
ok()    { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }

# ------------------------------------------------------------------
# Handle --abort
# ------------------------------------------------------------------
if [[ "${1:-}" == "--abort" ]]; then
    if git merge --abort 2>/dev/null; then
        ok "Merge aborted"
    else
        warn "No merge in progress to abort"
    fi
    # Revert any auto-resolve changes
    git checkout -- . 2>/dev/null || true
    exit 0
fi

# ------------------------------------------------------------------
# Pre-checks
# ------------------------------------------------------------------
if ! git remote get-url "$UPSTREAM_REMOTE" &>/dev/null; then
    err "Upstream remote '$UPSTREAM_REMOTE' not found."
    info "Add it: git remote add upstream https://github.com/NousResearch/hermes-agent.git"
    exit 1
fi

if ! git diff-index --quiet HEAD --; then
    warn "You have uncommitted changes. Stashing..."
    git stash --include-untracked
    STASHED=true
else
    STASHED=false
fi

# Make sure we're on main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    warn "You are on '$CURRENT_BRANCH', not 'main'. Switching..."
    git checkout main
fi

# ------------------------------------------------------------------
# Fetch upstream
# ------------------------------------------------------------------
info "Fetching $UPSTREAM_REMOTE/$UPSTREAM_BRANCH..."
git fetch "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH" 2>&1 | sed 's/^/  /'

# Check if there's anything new
MERGE_BASE=$(git merge-base HEAD "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH")
UPSTREAM_TIP=$(git rev-parse "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH")

if [[ "$MERGE_BASE" == "$UPSTREAM_TIP" ]]; then
    ok "Already up to date with upstream — nothing to merge."
    $STASHED && git stash pop 2>/dev/null || true
    exit 0
fi

info "Upstream has new commits ($(git rev-list --count $MERGE_BASE..$UPSTREAM_TIP) commits behind)"

# ------------------------------------------------------------------
# Check if we have local (atlaz-specific) commits since upstream
# ------------------------------------------------------------------
LOCAL_AHEAD=$(git rev-list --count "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"..HEAD 2>/dev/null || echo "0")
if [[ "$LOCAL_AHEAD" -gt 0 ]]; then
    ok "You have $LOCAL_AHEAD atlaz-specific commit(s) that are not in upstream"
fi

# ------------------------------------------------------------------
# Attempt merge + auto-resolve
# ------------------------------------------------------------------
info "Merging $UPSTREAM_REMOTE/$UPSTREAM_BRANCH..."

# Do the merge but don't commit yet
MERGE_EXIT=0
MERGE_OUTPUT=$(git merge "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" --no-commit 2>&1) || MERGE_EXIT=$?

if [[ "$MERGE_EXIT" -eq 0 ]]; then
    # No conflicts at all — clean merge
    ok "No conflicts — clean merge"
    git commit -m "$MERGE_MSG" --no-edit
    ok "Merge committed successfully"

    if [[ "${1:-}" == "--push" ]]; then
        info "Pushing to origin..."
        git push origin main
        ok "Pushed to origin/main"
    fi

    $STASHED && git stash pop 2>/dev/null || true
    exit 0
fi

# ------------------------------------------------------------------
# There are conflicts — analyze and auto-resolve
# ------------------------------------------------------------------
warn "Merge has conflicts. Analyzing..."

CONFLICT_FILES=$(git diff --name-only --diff-filter=U 2>/dev/null || true)
REAL_CONFLICTS=()
RENAME_ONLY=()

for file in $CONFLICT_FILES; do
    if [[ ! -f "$file" ]]; then
        continue
    fi

    # Get both sides of the conflict
    # Count conflicts: just hermes↔atlaz renames, or real changes?
    CONFLICT_LINES=$(grep -c '^<<<<<<< ' "$file" 2>/dev/null || echo "0")
    RENAME_LINES=$(grep -ci 'hermes\|Hermes' "$file" 2>/dev/null || echo "0")

    # Heuristic: if most conflict chunks are just renames, auto-resolve
    # by accepting our side (atlaz)
    #
    # We check: does upstream's version differ from ours only by
    # s/hermes/atlaz/g?  If yes → accept ours.
    # If there are structural changes → flag for review.

    # Get the conflict markers content — check if this is a complex conflict
    # by looking at how many non-rename differences exist
    OUR_LABEL="HEAD"
    UPSTREAM_LABEL="$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"

    # Check if this file is in our "always keep atlaz" list
    if grep -q "^$file " .gitattributes 2>/dev/null | grep -q "merge=atlaz-ours"; then
        info "  $file → marked as atlaz-custom (keeping our version)"
        git checkout --ours "$file"
        git add "$file"
        RENAME_ONLY+=("$file")
        continue
    fi

    # Try a smart merge: apply upstream's version and re-apply atlaz rebrand
    # If that resolves all conflicts, it was a rename-only conflict
    TMP_OURS=$(mktemp)
    TMP_THEIRS=$(mktemp)
    TMP_MERGED=$(mktemp)

    git show "HEAD:$file" > "$TMP_OURS" 2>/dev/null || true
    git show "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH:$file" > "$TMP_THEIRS" 2>/dev/null || true

    # Count lines that are the same between ours and theirs (ignoring renames)
    # Simple heuristic: diff3-style check
    SIMILARITY=$(diff <(sed 's/hermes/atlaz/g; s/Hermes/Atlaz/g; s/HERMES/ATLAZ/g' "$TMP_THEIRS") "$TMP_OURS" 2>/dev/null | wc -l)

    rm -f "$TMP_OURS" "$TMP_THEIRS" "$TMP_MERGED"

    if [[ "$SIMILARITY" -eq 0 ]]; then
        info "  $file → rename-only conflict (auto-resolved ✓)"
        git checkout --ours "$file"
        git add "$file"
        RENAME_ONLY+=("$file")
    else
        warn "  $file → REAL conflict ⚠️  ($CONFLICT_LINES conflict chunks)"
        REAL_CONFLICTS+=("$file")
    fi
done

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo ""
echo "========================================"
echo "  Merge Result Summary"
echo "========================================"
if [[ ${#RENAME_ONLY[@]} -gt 0 ]]; then
    echo ""
    ok "Auto-resolved (rename only):"
    for f in "${RENAME_ONLY[@]}"; do echo "    $f"; done
fi

if [[ ${#REAL_CONFLICTS[@]} -gt 0 ]]; then
    echo ""
    err "REAL CONFLICTS — manual resolution required:"
    for f in "${REAL_CONFLICTS[@]}"; do
        echo -e "    ${RED}$f${NC}"
    done
    echo ""
    echo "To resolve each file:"
    echo "  Edit the file, fix conflict markers, then: git add $file"
    echo "When all resolved: git commit"
    echo "To abort entirely:  git merge --abort"
    echo ""
    exit 1
else
    echo ""
    ok "All conflicts were rename-only — auto-resolved ✓"
    git commit -m "$MERGE_MSG" --no-edit
    ok "Merge committed successfully"

    if [[ "${1:-}" == "--push" ]]; then
        info "Pushing to origin..."
        git push origin main
        ok "Pushed to origin/main"
    fi
fi

$STASHED && git stash pop 2>/dev/null || true
