"""Public errors carry fixed codes and safe messages, never raw payloads."""


class BillingError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
