# Waking a Claude Code session

**Status.** The upstream hook example was confirmed on a different version and operating system. Windows desktop sessions are unverified.

## File hooks (recommended)

`SessionStart` creates an inbox for the current session and registers its `signal` file in `watchPaths`. The wakesub file adapter publishes the notice to that inbox, then touches `signal`. `FileChanged` reads pending notices, renames each one to `<id>.delivered`, writes the notices to stderr, and exits with code 2. Claude Code's `asyncRewake` can surface that output to an idle session as a reminder. If the inbox has no pending notices, the hook exits with code 0. Watching the signal alone keeps notice renames from generating another wake.

1. Open the directory containing the main wakesub README as the Claude Code project. Merge the two event groups in [hooks.json](hooks.json) into the project's local settings at `.claude/settings.local.json`. Preserve existing settings. The example uses `python` from `PATH`. If your installation uses `python3` or an absolute path to a Python executable, update both `command` fields.
2. Start or resume the session. The `SessionStart` context gives the session its own target, config path, and store path. From the project directory the session can register a wake, substituting those values for the placeholders below.

   ```sh
   python -B -m wakesub --store "SESSION_STORE" add --file finished.marker --target "SESSION_TARGET" --say "The worker's completion marker is present."
   ```

3. In a separate shell in the same project directory, run the poller with the same store and the generated config.

   ```sh
   python -B -m wakesub --store "SESSION_STORE" --config "SESSION_CONFIG" daemon
   ```

4. Let the Claude Code session idle and have the worker create `finished.marker`. To try it with an unused marker path, create the empty marker from another shell after the session is idle.

   ```sh
   python -B -c "from pathlib import Path; Path('finished.marker').touch()"
   ```

   The poller reports a file handoff. Look for the notice in the session to confirm the wake.

Each session uses a separate config and store in this example. The poller confirms the file handoff even if the receiving session has stopped. A watcher can miss signals while the session is closed, and pending notices are picked up on a later signal. The rename happens before reporting the notice, so interruption in between can leave a `.delivered` file without a reminder. Keep the inbox writable only by trusted processes. `prune` acts on subscription records in the store. Inbox files have their own lifetime.

The reference describes the hook fields in the example. For your release, verify against current hooks reference that `FileChanged` supports `asyncRewake` on a path supplied by `SessionStart.watchPaths` and that an idle desktop session surfaces its stderr on exit 2. [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)

## The session's own messaging socket

Claude Code exposes `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` to its own hooks and shell commands. When the session registers a subscription, it can supply these values to a trusted local adapter for later delivery to that same session. The values can change when the app or session restarts. The receiving session sees the notice as a message from another session, which carries no user approval. For transport details, see the official documentation. [Cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)
