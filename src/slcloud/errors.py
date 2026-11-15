"""Shared error types for slcloud."""


class NotFound(Exception):
    """A requested workspace, task, lesson or key does not exist."""


class HandleTaken(ValueError):
    """A member handle is already used by another member (handles are unique across all workspaces)."""

    def __init__(self, handle: str = ""):
        super().__init__("handle taken")
        self.handle = handle


class BadTransition(Exception):
    """A task status change the engine does not allow."""


class Rejected(Exception):
    """A request was refused on its content."""


class Forbidden(Exception):
    """The caller's role does not allow the action."""
