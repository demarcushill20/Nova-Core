#!/usr/bin/env bash
# Install NovaCore git hooks. Run once after cloning.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_DIR="$REPO_ROOT/.git/hooks"

cat > "$HOOK_DIR/pre-commit" << 'HOOK'
#!/usr/bin/env bash
# NovaCore pre-commit: block runtime artifacts and secrets.
exec "$(git rev-parse --show-toplevel)/scripts/check-guardrails.sh"
HOOK

chmod +x "$HOOK_DIR/pre-commit"
echo "Installed pre-commit hook at $HOOK_DIR/pre-commit"
