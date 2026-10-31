class ExecutionUncertain(RuntimeError):
    """Execution may still be active; reconcile before reusing the session."""


class RetryLater(RuntimeError):
    """Pause only at a known-safe boundary; never retry an ambiguous model call."""
    def __init__(self, delay=15):
        self.delay = max(1, delay)
        super().__init__("Work deferred by a runtime limit")


class TaskFailed(RuntimeError):
    """A task could not satisfy its goal within its execution budget."""
