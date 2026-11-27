"""Read pending inbox messages after the current session's signal changes."""

import json
import re
import sys
from pathlib import Path

from on_session_start import session_inbox


NOTICE_NAME = re.compile(r"[0-9a-f]{32}\.md")


def main():
    event = json.load(sys.stdin.buffer)
    inbox = session_inbox(event["session_id"])
    if Path(event["file_path"]) != inbox / "signal" or event["event"] not in ("add", "change"):
        return 0
    messages = []
    had_error = False
    for path in sorted(inbox.iterdir()):
        if not NOTICE_NAME.fullmatch(path.name):
            continue
        try:
            message = path.read_text(encoding="utf-8")
            path.rename(path.with_suffix(".delivered"))
        except FileNotFoundError:
            # Another invocation already claimed this notice.
            continue
        except (OSError, UnicodeError):
            had_error = True
        else:
            messages.append(message.rstrip("\n"))
    if had_error:
        print("A wakesub inbox file could not be read or marked delivered.", file=sys.stderr)
    if messages:
        sys.stderr.buffer.write(("\n\n".join(messages) + "\n").encode("utf-8"))
        return 2
    return int(had_error)


if __name__ == "__main__":
    sys.exit(main())
