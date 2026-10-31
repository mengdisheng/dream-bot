from __future__ import annotations

import uuid


class BotError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retryable: bool = False):
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
        self.diagnostic_id = str(uuid.uuid4())
        super().__init__(message)

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "retryable": self.retryable, "diagnostic_id": self.diagnostic_id}}
