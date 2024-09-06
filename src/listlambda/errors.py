"""Domain errors. The API layer maps each to an HTTP status code."""


class ListLambdaError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(ListLambdaError):
    status_code = 400
    code = "validation_error"


class UnauthorizedError(ListLambdaError):
    status_code = 401
    code = "unauthorized"


class NotFoundError(ListLambdaError):
    status_code = 404
    code = "not_found"


class ConflictError(ListLambdaError):
    status_code = 409
    code = "conflict"
