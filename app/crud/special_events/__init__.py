"""Special events (one-off paid classes): events, seat holds/tickets, and schedule takeovers.

See ``app/models/specialEventModel.py`` for the data model and its invariants.
"""
from . import events, orders, takeover  # noqa: F401
from .common import SpecialEventError  # noqa: F401
