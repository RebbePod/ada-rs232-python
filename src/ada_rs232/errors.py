"""Public library exceptions."""


class AdaError(Exception):
    """Base exception for amplifier communication errors."""


class AdaConnectionError(AdaError):
    """A connection, write, or amplifier response failed."""
