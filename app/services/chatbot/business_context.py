"""Read-only business context shared by chatbot and template-writing AI."""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import usersCrud, venuesCrud
from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.crud.memberships import plans as plans_crud
from app.crud.standing_bookings import catalog as class_catalog
from app.models import Venue

_WEEKDAYS_ES = ["Domingo", "Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado"]


async def build_business_info(db: AsyncSession, config: ChatbotConfigData) -> str:
    """Render configured business info, with venue address fallback."""
    lines: List[str] = []
    if config.business_name:
        lines.append(f"Nombre: {config.business_name}")
    address = config.address
    if not address:
        venue = (await db.execute(select(Venue).order_by(Venue.id).limit(1))).scalars().first()
        address = venue.address if venue else None
    if address:
        lines.append(f"Direccion: {address}")
    if config.operating_hours:
        lines.append(f"Horarios: {config.operating_hours}")
    if config.phone:
        lines.append(f"Telefono: {config.phone}")
    if config.policies:
        lines.append(f"Politicas: {config.policies}")
    if config.extra_info:
        lines.append(config.extra_info)
    return "\n".join(lines)


async def build_template_writer_context(db: AsyncSession, config: ChatbotConfigData) -> str:
    """Build compact live context for WhatsApp template copywriting."""
    sections: List[str] = []
    business_info = await build_business_info(db, config)
    if business_info:
        sections.append("Informacion configurada:\n" + business_info)
    if config.system_prompt:
        sections.append("Reglas del chatbot:\n" + config.system_prompt.strip())
    if config.tone:
        sections.append(f"Tono configurado: {config.tone}")

    plans = await plans_crud.get_membership_plans(db)
    if plans:
        lines = []
        for plan in plans[:20]:
            if plan.duration_unit == "day":
                kind = "pase diario"
            elif plan.fixed_time_slot:
                kind = "paquete con horario fijo"
            else:
                kind = "membresia"
            desc = f" - {plan.description}" if plan.description else ""
            lines.append(
                f"{plan.name} ({kind}): ${plan.price:.2f}, "
                f"{plan.duration_value} {plan.duration_unit}(s){desc}"
            )
        sections.append("Planes disponibles:\n" + "\n".join(lines))

    templates = await class_catalog.get_class_templates(db, active_only=True)
    if templates:
        lines = []
        for template in templates[:40]:
            day = (
                _WEEKDAYS_ES[template.weekday]
                if isinstance(template.weekday, int) and 0 <= template.weekday < 7
                else f"Dia {template.weekday}"
            )
            hhmm = (template.start_time_local or "")[:5]
            cls = template.class_type_name or template.name or "Clase"
            venue = f" en {template.venue_name}" if template.venue_name else ""
            instr = f" con {template.instructor_name}" if template.instructor_name else ""
            lines.append(f"{day} {hhmm}: {cls}{venue}{instr}")
        sections.append("Horario semanal recurrente:\n" + "\n".join(lines))

    venues = await venuesCrud.list_venues(db)
    if venues:
        lines = []
        for venue in venues[:10]:
            address = f" - {venue.address}" if venue.address else ""
            desc = f" ({venue.description})" if venue.description else ""
            lines.append(f"{venue.name}{address}; capacidad {venue.capacity}{desc}")
        sections.append("Sedes:\n" + "\n".join(lines))

    instructors = await usersCrud.list_people(db, role_code="instructor")
    names = sorted({person.full_name for person in instructors if getattr(person, "full_name", None)})
    if names:
        sections.append("Instructores:\n" + ", ".join(names[:30]))

    return "\n\n".join(sections)
