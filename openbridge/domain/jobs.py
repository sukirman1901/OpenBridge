"""Execution lanes isolate session ordering, not host filesystem access."""
LANES = ("interactive", "background")


def execution_lane(kind):
    if kind == "telegram_update":
        return "interactive"
    if kind in ("schedule", "task", "initiative"):
        return "background"
    raise ValueError("Unknown job kind has no execution lane")
