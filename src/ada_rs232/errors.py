"""Public library exceptions."""


class AdaError(Exception):
    """Base exception for amplifier communication errors."""


class AdaConnectionError(AdaError):
    """A connection, write, or amplifier response failed."""


class AdaSetupError(AdaError):
    """Setup mode is required, active, or returned unconfirmed settings."""
