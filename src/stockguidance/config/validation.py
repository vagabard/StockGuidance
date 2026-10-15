"""Scalar checks shared by externally constructed configuration contracts."""

from datetime import date, datetime
from enum import Enum


def require_text(value: object, name: str) -> None:
	if not isinstance(value, str) or not value.strip():
		raise ValueError(f"{name} must be a nonempty string")


def require_enum[EnumType: Enum](value: object, enum_type: type[EnumType], name: str) -> None:
	if not isinstance(value, enum_type):
		raise ValueError(f"{name} must be a {enum_type.__name__}")


def require_date(value: object, name: str) -> None:
	if not isinstance(value, date) or isinstance(value, datetime):
		raise ValueError(f"{name} must be a session date, not a timestamp")


def require_aware_datetime(value: datetime, name: str) -> None:
	if not isinstance(value, datetime) or value.utcoffset() is None:
		raise ValueError(f"{name} must be a timezone-aware datetime")


def require_positive_int(value: object, name: str) -> None:
	if type(value) is not int or value <= 0:
		raise ValueError(f"{name} must be a positive integer")
