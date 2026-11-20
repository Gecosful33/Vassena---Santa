"""Errors whose messages are safe to print in a public CI log.

The repository is public and so are its workflow logs. A message carried by
these exceptions must describe *what went wrong structurally* and never quote
page content, credentials, guest names or amounts.
"""

from __future__ import annotations


class PortalError(RuntimeError):
    """Base class: a manager's portal could not be read."""


class TransportError(PortalError):
    """The portal did not answer, or answered with an unexpected HTTP status."""


class LoginError(PortalError):
    """The portal refused the credentials."""


class LayoutError(PortalError):
    """A page no longer has the structure the parser was written for."""


class SyncAborted(RuntimeError):
    """Nothing fresh could be read: the page already online is left untouched."""
