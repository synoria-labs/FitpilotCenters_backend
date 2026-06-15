"""CRUD for venues (read-only reference data).

Lightweight listing used by the chatbot's ``get_venues`` tool to answer "where are you located?"
and capacity questions from live DB data instead of static config.
"""
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Venue


@dataclass
class VenueData:
    id: int
    name: str
    address: Optional[str]
    capacity: int
    description: Optional[str]


async def list_venues(db: AsyncSession) -> List[VenueData]:
    """List all venues (name, address, capacity, description), ordered by name."""
    result = await db.execute(select(Venue).order_by(Venue.name))
    venues = result.scalars().all()
    return [
        VenueData(
            id=v.id,
            name=v.name,
            address=v.address,
            capacity=v.capacity,
            description=v.description,
        )
        for v in venues
    ]
