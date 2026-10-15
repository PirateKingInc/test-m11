class IngestError(Exception):
    """Base class for ingestion failures that should fail one job, not the process."""


class QuotaExceeded(IngestError):
    """The call would exceed the free-tier budget (or today's paced allowance)."""


class SourceUnavailable(IngestError):
    """Circuit breaker is open for this source."""


class ApiError(IngestError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ParseError(IngestError):
    """Response did not match the expected shape (e.g. an unofficial API changed format)."""


class ConfigError(IngestError):
    """Missing API key or bad configuration for a source."""
