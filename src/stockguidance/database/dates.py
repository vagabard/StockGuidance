from datetime import date, datetime


def normalize_session_date(value: object) -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, bytes):
		value = value.decode("ascii")
	if isinstance(value, str):
		return datetime.fromisoformat(value).date()
	raise ValueError("Unsupported database session-date representation")


def normalize_date_id(value: object) -> int:
	if isinstance(value, bytes):
		value = value.decode("ascii")
	if isinstance(value, str) and value.isascii() and value.isdecimal():
		value = int(value)
	if type(value) is not int or value <= 0:
		raise ValueError("Database date ID must be a positive integer")
	return value
