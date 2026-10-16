from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from stockguidance.config.validation import require_text
from stockguidance.reporting.recommendations import render_recommendations
from stockguidance.workflows.recommendation_result import RecommendationRunResult


@dataclass(frozen=True)
class EmailDraft:
	sender: str
	recipients: tuple[str, ...]
	subject: str
	body: str

	def __post_init__(self) -> None:
		if not isinstance(self.recipients, tuple) or not self.recipients:
			raise ValueError("Email requires explicit immutable recipients")
		for value in (self.sender, *self.recipients, self.subject):
			require_text(value, "email header")
			if "\r" in value or "\n" in value:
				raise ValueError("Email headers cannot contain newlines")
		require_text(self.body, "email body")

	def message(self) -> EmailMessage:
		message = EmailMessage()
		message["From"], message["To"], message["Subject"] = self.sender, ", ".join(self.recipients), self.subject
		message.set_content(self.body)
		return message


class MailTransport(Protocol):
	def send_message(self, message: EmailMessage) -> object: ...


def recommendation_draft(run: RecommendationRunResult, sender: str, recipients: tuple[str, ...]) -> EmailDraft:
	return EmailDraft(sender, recipients, f"Close-to-close plan for {run.recommendation.request.timing.entry_session.session_date}", render_recommendations(run))


def send_draft(draft: EmailDraft, transport: MailTransport) -> None:
	transport.send_message(draft.message())
