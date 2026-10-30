"""Read-only, explicitly bounded repositories.

The feature repository imports the operation-recovery guards.  Keep this
package initializer lazy so importing another repository (for example the
pending-operation repository used by recovery) does not create a circular
import before either module has finished defining its contracts.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from stockguidance.database.repositories.features import FeatureChannelReadError, MySQLFeatureChannelRepository

__all__ = ["FeatureChannelReadError", "MySQLFeatureChannelRepository"]


def __getattr__(name: str) -> object:
	if name in __all__:
		from stockguidance.database.repositories.features import FeatureChannelReadError, MySQLFeatureChannelRepository
		return {"FeatureChannelReadError": FeatureChannelReadError, "MySQLFeatureChannelRepository": MySQLFeatureChannelRepository}[name]
	raise AttributeError(name)
