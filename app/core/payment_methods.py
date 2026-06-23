"""Canonical payment methods shared by the POS write paths.

Historical ``payments.method`` rows are free-form strings (a mix of ``cash``,
``efectivo``, ``card``, ``transfer``, ``mercadopago``) and are intentionally NOT
migrated. Canonicalization is enforced only on the new POS write paths so that
nothing existing breaks.

``CASH_METHODS`` is the set of methods that move physical cash in the drawer; it
is what the *corte de caja* counts. It tolerates the legacy Spanish value
``efectivo`` still present in old rows.
"""
from __future__ import annotations

from typing import Optional

CASH = "cash"
CARD = "card"
TRANSFER = "transfer"
MERCADOPAGO = "mercadopago"
OTHER = "other"
# Sentinel used as a membership anchor payment's method when a single sale is
# settled with more than one tender (the true breakdown lives in sale_payments).
MIXED = "mixed"

ALL_METHODS = [CASH, CARD, TRANSFER, MERCADOPAGO, OTHER]

# Methods that count toward the cash drawer for the corte de caja.
CASH_METHODS = {CASH, "efectivo"}

LABELS = {
    CASH: "Efectivo",
    CARD: "Tarjeta",
    TRANSFER: "Transferencia",
    MERCADOPAGO: "MercadoPago",
    OTHER: "Otro",
    MIXED: "Mixto",
}


def is_cash(method: Optional[str]) -> bool:
    """True when ``method`` moves physical cash (counts for the corte de caja)."""
    return (method or "").strip().lower() in CASH_METHODS


def label_for(method: Optional[str]) -> str:
    """Human-friendly label, falling back to the raw value for unknown methods."""
    key = (method or "").strip().lower()
    return LABELS.get(key, method or "")
