"""Small safeguards shared by state and event output."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class SecretRedactor:
    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(sorted({value for value in secrets if value}, key=len, reverse=True))

    def text(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [self.value(item) for item in value]
        return value
