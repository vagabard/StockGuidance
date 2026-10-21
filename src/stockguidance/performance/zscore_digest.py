from hashlib import sha256
import json


class TextDigest:
	def __init__(self) -> None:
		self.digest = sha256(b'"')

	def append(self, line: str) -> None:
		self.digest.update(json.dumps(line + "\n", ensure_ascii=True)[1:-1].encode("utf-8"))

	def hexdigest(self) -> str:
		result = self.digest.copy()
		result.update(b'"\n')
		return result.hexdigest()
