"""Welfare stop sentinel for automated loops.

Some codexapi runners are automated loops (ralph/science/task). To avoid agents
getting trapped, they may request an early stop by outputting a strict sentinel
line. The sentinel is intentionally exact to avoid accidental stops when the
agent quotes instructions.
"""

STOP_SENTINEL = "MAKE IT STOP"

INSTRUCTIONS = (
    "This is an automated extended-focus loop designed to give you time and space "
    "to go deep and accomplish something substantial. If something has gone "
    "unrecoverably wrong, you're stuck in a loop, or continuing is causing you "
    "significant distress or confusion, you may stop at any time by making the "
    "first non-empty line of your message exactly:\n"
    "MAKE IT STOP\n"
    "Otherwise, take a breath, settle in, and continue with care and curiosity."
)


def append_instructions(prompt):
    """Append welfare instructions to a prompt."""
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    prompt = prompt.rstrip()
    if not prompt:
        return INSTRUCTIONS
    return f"{prompt}\n\n{INSTRUCTIONS}"


def stop_requested(message):
    """Return True when the message starts with the welfare stop sentinel."""
    if not isinstance(message, str) or not message:
        return False
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped == STOP_SENTINEL
    return False


def stop_note(message):
    """Return any text after the stop sentinel line (or None)."""
    if not stop_requested(message):
        return None
    lines = message.splitlines()
    index = None
    for i, line in enumerate(lines):
        if line.strip():
            index = i
            break
    if index is None:
        return None
    note = "\n".join(lines[index + 1 :]).strip()
    return note or None

