"""Register one signal file for the current session."""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


def session_inbox(session_id):
    root = Path(os.environ["CLAUDE_PROJECT_DIR"]).resolve()
    target = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root / ".wakesub" / "claude-code" / target


def main():
    event = json.load(sys.stdin.buffer)
    inbox = session_inbox(event["session_id"])
    inbox.mkdir(parents=True, exist_ok=True)
    config = inbox / "wakesub.json"
    if not config.exists():
        descriptor, name = tempfile.mkstemp(dir=inbox, prefix=".config-")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"adapter": {"type": "file", "inbox": ".", "signal": True}}, stream)
            if not config.exists():
                temporary.replace(config)
        finally:
            temporary.unlink(missing_ok=True)
    signal = inbox / "signal"
    try:
        signal.touch(exist_ok=False)
    except FileExistsError:
        pass
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "watchPaths": [str(signal)],
            "additionalContext": (
                f"wakesub file delivery is configured for target {inbox.name}. "
                f"The config is at {config}. The subscription store is at {inbox / 'store'}."
            ),
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
