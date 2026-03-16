# lsp-hooks

## Rules

- **Always bump the version in `.claude-plugin/plugin.json` on every change.** Claude Code uses the version to decide whether to update the plugin cache. No version bump = no cache update for end users, even with `autoUpdate: true`. Use semver: patch for fixes, minor for features.
