"""Request-scoped read identity using the store's existing authorization epochs."""

from functools import wraps
from typing import Any, Callable, FrozenSet

from .store import ObservationStore


class ReadAuthorizationExpired(PermissionError):
    """The captured read identity was revoked, deleted or superseded."""


class ReadAuthorization:
    def __init__(self, store: ObservationStore) -> None:
        self.store = store
        self.epoch, self.session_epochs = store.read_authorization_snapshot()
        self.sessions: FrozenSet[str] = frozenset(self.session_epochs)

    def verify(self) -> None:
        if not self.store.read_authorization_valid(self.epoch, self.session_epochs):
            raise ReadAuthorizationExpired("Observation read authorization changed")


def authorized_read(function: Callable[..., Any]) -> Callable[..., Any]:
    """Check once around a public query, never once per SQL row or across I/O."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        authorization = self.read_authorization
        if authorization is not None:
            authorization.verify()
        result = function(self, *args, **kwargs)
        if authorization is not None:
            authorization.verify()
        return result

    return wrapped
