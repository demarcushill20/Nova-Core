#!/usr/bin/env bash
# Install NovaCore git hooks. Run once after cloning.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_DIR="$REPO_ROOT/.git/hooks"

cat > "$HOOK_DIR/pre-commit" << 'HOOK'
#!/usr/bin/env bash
# NovaCore pre-commit: guardrails + NovaTrade quality gate.
REPO="$(git rev-parse --show-toplevel)"

"$REPO/scripts/check-guardrails.sh" || exit 1

# NovaTrade quality gate (only if novatrade/ files are staged)
if git diff --cached --name-only | grep -q '^novatrade/'; then
  python3 "$REPO/scripts/check-novatrade-quality.py" --staged || exit 1
fi
HOOK

chmod +x "$HOOK_DIR/pre-commit"
echo "Installed pre-commit hook at $HOOK_DIR/pre-commit"
