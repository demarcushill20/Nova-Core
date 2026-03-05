# NovaCore Security

## Guardrails

NovaCore has two layers of protection against accidental secret or artifact commits:

### 1. Pre-commit hook (local)

Install once after cloning:

```bash
./scripts/install-hooks.sh
```

This installs a pre-commit hook that runs `scripts/check-guardrails.sh` on every `git commit`. It blocks:

- **Runtime artifacts**: files in `TASKS/`, `OUTPUT/`, `WORK/`, `STATE/` (except `.gitkeep` and `STATE/tools_registry.json`), and `HEARTBEAT.md`
- **Secret patterns**: Telegram bot tokens, GitHub PATs, AWS keys, Slack tokens, OpenAI/Anthropic API keys
- **Environment files**: any `*.env` file

The hook never prints secret values — only file paths, line numbers, and pattern types.

### 2. GitHub Actions CI

The `.github/workflows/guardrails.yml` workflow runs the same checks on every push and PR to `main`. It also runs the test suite.

### Running checks manually

```bash
# Check staged files (same as pre-commit)
scripts/check-guardrails.sh

# Check all tracked files (same as CI)
scripts/check-guardrails.sh --all
```

## Token rotation

If a secret is detected in git history or the working tree:

1. **Telegram bot token**: Revoke via [@BotFather](https://t.me/BotFather) → `/revoke`, generate a new token, update `/etc/novacore/telegram.env`
2. **GitHub PAT**: Revoke at GitHub → Settings → Developer settings → Personal access tokens, generate a new one
3. **AWS keys**: Deactivate in IAM console, create a new key pair
4. **OpenAI/Anthropic keys**: Rotate in the respective API dashboard

After rotation, restart affected services:

```bash
sudo systemctl restart novacore-telegram.service
sudo systemctl restart novacore-telegram-notifier.service
```

**Important**: Rewriting git history does NOT revoke compromised tokens. Always rotate first, then rewrite.

## History rewrite (if needed)

If runtime artifacts or secrets need to be purged from git history:

### Prerequisites

```bash
pip install git-filter-repo
```

### Steps

1. **Create a mirror backup**:
   ```bash
   git clone --mirror git@github.com:demarcushill20/Nova-Core.git Nova-Core-backup.git
   ```

2. **Run filter-repo** to remove runtime paths:
   ```bash
   git filter-repo \
     --invert-paths \
     --path TASKS/ \
     --path OUTPUT/ \
     --path WORK/ \
     --path HEARTBEAT.md \
     --path-regex '^STATE/(?!tools_registry\.json$|\.gitkeep$)' \
     --force
   ```

3. **Verify removal**:
   ```bash
   # Should return 0 for each
   git log --all --name-only -- TASKS/ | grep -c 'TASKS/'
   git log --all --name-only -- OUTPUT/ | grep -c 'OUTPUT/'
   git log --all --name-only -- WORK/ | grep -c 'WORK/'
   ```

4. **Force-push**:
   ```bash
   git push origin --force --all
   git push origin --force --tags
   ```

5. **Post-push cleanup**: Ask all collaborators to re-clone (not pull). GitHub caches objects; to request garbage collection, contact GitHub support or wait for automatic GC.

### Rollback

If the rewrite goes wrong:

```bash
# From the mirror backup
cd Nova-Core-backup.git
git push --mirror git@github.com:demarcushill20/Nova-Core.git
```

### Warnings

- Force-push rewrites all commit hashes — open PRs will break
- All collaborators must re-clone after a history rewrite
- GitHub may cache objects for up to 90 days even after force-push
- History rewrite does NOT revoke leaked secrets — always rotate tokens first
