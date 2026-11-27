# Changelog

## Unreleased

- Add one-shot subscriptions for time, file presence, file content, and predecessor completion.
- Add command, inbox-file, and no-op delivery adapters.
- Persist expiry, bounded retry outcomes, and per-target throttling in a local store.
- Provide one-cycle execution and a foreground polling worker.
- Show three ways an agent session can subscribe to its own wake in the English and Korean READMEs.
- Add a Claude Code file hook example, optional inbox signaling, and a reference for delivery through a session's own socket.
- Add `prune` with configurable retention for completed and failed subscriptions.
