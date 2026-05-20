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


class HHOAuthError(HHApiError):
    def __init__(
        self, message: str, status_code: int = 401, body: dict[str, Any] | str = ""
    ) -> None:
        super().__init__(status_code, body)
        self.message = message

    def __str__(self) -> str:
        return self.message
