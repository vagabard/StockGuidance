"""Small database protocols shared by target and historical repositories."""

from collections.abc import Sequence
from typing import Protocol


class Cursor(Protocol):
	def execute(self, operation: str, parameters: tuple[object, ...]) -> object: ...
	def fetchall(self) -> Sequence[Sequence[object]]: ...
	def close(self) -> None: ...
