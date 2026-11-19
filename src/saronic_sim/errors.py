"""Errors shared by request parsing, world configuration, and the simulation thread."""


class CommandError(ValueError):
    """A request or command that cannot be applied, with a stable code and the offending field."""

    def __init__(self, message, code="invalid_request", field=None):
        super().__init__(message)
        self.code = code
        self.field = field

    def body(self):
        body = {"code": self.code, "message": str(self)}
        if self.field:
            body["field"] = self.field
        return body
