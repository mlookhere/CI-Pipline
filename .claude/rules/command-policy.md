# Command and release policy

- Never use `--no-verify`, force-push, admin-merge, hard reset, destructive `git clean`, broad `rm -rf`, `chmod 777`, or Docker system prune.
- Never pipe remote downloads directly into a shell.
- Never persist credentials through Git config or commit secrets, tokens, cookies, private keys, `.env` files, or production data.
- Push only the current task branch after local gates. Never push directly to integration or production branches.
- Repository secret/variable administration, destructive infrastructure operations, package publication, cluster changes, and production deployment are human-supervised operations.
- The deterministic hooks and GitHub rulesets are authoritative even when these instructions are omitted from context.
