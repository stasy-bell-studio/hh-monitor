from typing import Any


class HHApiError(Exception):
    def __init__(self, status_code: int, body: dict[str, Any] | str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HH API error {status_code}: {body}")


class HHQuotaExceeded(HHApiError):
    pass


class HHServiceNotActive(HHApiError):
    pass


class HHNotFound(HHApiError):
    pass


class HHRateLimit(HHApiError):
    def __init__(
        self, status_code: int, body: dict[str, Any] | str, retry_after_seconds: float
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(status_code, body)


class HHViewLimitExceeded(HHApiError):
    """Daily quota for resume views exhausted; resets at 00:00 MSK."""


class SearchNotFoundError(Exception):
    """Raised when a Search row with the given id does not exist."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class HHOAuthError(HHApiError):
    def __init__(
        self, message: str, status_code: int = 401, body: dict[str, Any] | str = ""
    ) -> None:
        super().__init__(status_code, body)
        self.message = message

    def __str__(self) -> str:
        return self.message


class LlmApiError(Exception):
    """Raised when the LLM API returns an unexpected HTTP error (not 401/429)."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"LLM API error {status_code}: {body}")


class LlmAuthError(LlmApiError):
    """Raised on HTTP 401 from the LLM API — invalid or missing API key."""

    def __init__(self, body: str = "") -> None:
        super().__init__(401, body)
