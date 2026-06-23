from datetime import datetime
from typing import List, Optional

import strawberry

from app.models.posModel import (
    CashMovement as CashMovementModel,
    CashSession as CashSessionModel,
    Product as ProductModel,
    Sale as SaleModel,
    SaleLineItem as SaleLineItemModel,
    SalePayment as SalePaymentModel,
)


@strawberry.type
class ProductType:
    id: int
    name: str
    sku: Optional[str]
    price: float
    track_stock: bool
    stock_qty: Optional[int]
    is_active: bool

    @classmethod
    def from_model(cls, p: ProductModel) -> "ProductType":
        return cls(
            id=p.id,
            name=p.name,
            sku=p.sku,
            price=float(p.price or 0),
            track_stock=bool(p.track_stock),
            stock_qty=p.stock_qty,
            is_active=bool(p.is_active),
        )


@strawberry.input
class CreateProductInput:
    name: str
    price: float
    sku: Optional[str] = None
    track_stock: bool = False
    stock_qty: Optional[int] = None
    is_active: bool = True


@strawberry.input
class UpdateProductInput:
    product_id: int
    name: Optional[str] = None
    price: Optional[float] = None
    sku: Optional[str] = None
    track_stock: Optional[bool] = None
    stock_qty: Optional[int] = None
    is_active: Optional[bool] = None


@strawberry.type
class ProductMutationResponse:
    success: bool
    product: Optional["ProductType"]
    message: str


@strawberry.type
class CashSessionType:
    id: int
    opened_by: Optional[int]
    opened_at: datetime
    opening_float: float
    closed_by: Optional[int]
    closed_at: Optional[datetime]
    status: str
    expected_cash: Optional[float]
    counted_cash: Optional[float]
    difference: Optional[float]
    notes: Optional[str]

    @classmethod
    def from_model(cls, s: CashSessionModel) -> "CashSessionType":
        return cls(
            id=s.id,
            opened_by=s.opened_by,
            opened_at=s.opened_at,
            opening_float=float(s.opening_float or 0),
            closed_by=s.closed_by,
            closed_at=s.closed_at,
            status=s.status,
            expected_cash=float(s.expected_cash) if s.expected_cash is not None else None,
            counted_cash=float(s.counted_cash) if s.counted_cash is not None else None,
            difference=float(s.difference) if s.difference is not None else None,
            notes=s.notes,
        )


@strawberry.type
class CashMovementType:
    id: int
    cash_session_id: int
    direction: str
    amount: float
    reason: Optional[str]
    created_at: datetime

    @classmethod
    def from_model(cls, m: CashMovementModel) -> "CashMovementType":
        return cls(
            id=m.id,
            cash_session_id=m.cash_session_id,
            direction=m.direction,
            amount=float(m.amount or 0),
            reason=m.reason,
            created_at=m.created_at,
        )


@strawberry.type
class SaleLineItemType:
    id: int
    line_type: str
    description: str
    quantity: int
    unit_price: float
    discount: float
    line_total: float
    plan_id: Optional[int]
    product_id: Optional[int]
    subscription_id: Optional[int]
    payment_id: Optional[int]

    @classmethod
    def from_model(cls, li: SaleLineItemModel) -> "SaleLineItemType":
        return cls(
            id=li.id,
            line_type=li.line_type,
            description=li.description,
            quantity=li.quantity,
            unit_price=float(li.unit_price or 0),
            discount=float(li.discount or 0),
            line_total=float(li.line_total or 0),
            plan_id=li.plan_id,
            product_id=li.product_id,
            subscription_id=li.subscription_id,
            payment_id=li.payment_id,
        )


@strawberry.type
class SalePaymentType:
    id: int
    amount: float
    method: str
    payment_id: Optional[int]
    created_at: datetime

    @classmethod
    def from_model(cls, sp: SalePaymentModel) -> "SalePaymentType":
        return cls(
            id=sp.id,
            amount=float(sp.amount or 0),
            method=sp.method,
            payment_id=sp.payment_id,
            created_at=sp.created_at,
        )


@strawberry.type
class SaleType:
    id: int
    person_id: Optional[int]
    person_name: Optional[str]
    cash_session_id: Optional[int]
    status: str
    subtotal: float
    discount_total: float
    tax_total: float
    total: float
    amount_paid: float
    change_due: float
    note: Optional[str]
    sold_by: Optional[int]
    created_at: datetime
    completed_at: Optional[datetime]
    line_items: List[SaleLineItemType]
    payments: List[SalePaymentType]

    @classmethod
    def from_model(cls, sale: SaleModel) -> "SaleType":
        person_name = None
        if "person" in sale.__dict__ and sale.person is not None:
            person_name = sale.person.full_name
        total = float(sale.total or 0)
        paid = float(sale.amount_paid or 0)
        return cls(
            id=sale.id,
            person_id=sale.person_id,
            person_name=person_name,
            cash_session_id=sale.cash_session_id,
            status=sale.status,
            subtotal=float(sale.subtotal or 0),
            discount_total=float(sale.discount_total or 0),
            tax_total=float(sale.tax_total or 0),
            total=total,
            amount_paid=paid,
            change_due=max(paid - total, 0.0),
            note=sale.note,
            sold_by=sale.sold_by,
            created_at=sale.created_at,
            completed_at=sale.completed_at,
            line_items=[SaleLineItemType.from_model(li) for li in (sale.line_items or [])],
            payments=[SalePaymentType.from_model(sp) for sp in (sale.sale_payments or [])],
        )


@strawberry.type
class MethodTotalType:
    method: str
    count: int
    total: float


@strawberry.type
class CashSessionReportType:
    session_id: int
    status: str
    opened_by: Optional[int]
    opened_at: datetime
    closed_at: Optional[datetime]
    opening_float: float
    sales_count: int
    sales_total: float
    cash_in: float
    cash_out: float
    cash_sales_total: float
    computed_expected_cash: float
    expected_cash: Optional[float]
    counted_cash: Optional[float]
    difference: Optional[float]
    by_method: List[MethodTotalType]

    @classmethod
    def from_data(cls, d) -> "CashSessionReportType":
        return cls(
            session_id=d.session_id,
            status=d.status,
            opened_by=d.opened_by,
            opened_at=d.opened_at,
            closed_at=d.closed_at,
            opening_float=d.opening_float,
            sales_count=d.sales_count,
            sales_total=d.sales_total,
            cash_in=d.cash_in,
            cash_out=d.cash_out,
            cash_sales_total=d.cash_sales_total,
            computed_expected_cash=d.computed_expected_cash,
            expected_cash=d.expected_cash,
            counted_cash=d.counted_cash,
            difference=d.difference,
            by_method=[
                MethodTotalType(method=b.method, count=b.count, total=b.total)
                for b in d.by_method
            ],
        )


# ---------------------------------------------------------------- inputs ----
@strawberry.input
class SaleLineInputType:
    line_type: str  # membership_new | membership_renewal | product | manual
    description: Optional[str] = None
    quantity: int = 1
    unit_price: Optional[float] = None
    discount: Optional[float] = 0
    plan_id: Optional[int] = None
    member_id: Optional[int] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    start_at: Optional[datetime] = None
    template_id: Optional[int] = None
    seat_id: Optional[int] = None
    product_id: Optional[int] = None


@strawberry.input
class SalePaymentInputType:
    method: str
    amount: float
    provider: Optional[str] = None
    provider_payment_id: Optional[str] = None
    external_reference: Optional[str] = None


@strawberry.input
class CreateSaleInput:
    line_items: List[SaleLineInputType]
    payments: List[SalePaymentInputType]
    person_id: Optional[int] = None
    note: Optional[str] = None


@strawberry.input
class OpenCashSessionInput:
    opening_float: float = 0
    notes: Optional[str] = None


@strawberry.input
class CloseCashSessionInput:
    cash_session_id: int
    counted_cash: float
    notes: Optional[str] = None


@strawberry.input
class CashMovementInput:
    cash_session_id: int
    direction: str  # 'in' | 'out'
    amount: float
    reason: Optional[str] = None


# ------------------------------------------------------------- responses ----
@strawberry.type
class SaleResponse:
    success: bool
    sale: Optional[SaleType]
    message: str


@strawberry.type
class CashSessionResponse:
    success: bool
    session: Optional[CashSessionType]
    message: str


@strawberry.type
class CashMovementResponse:
    success: bool
    movement: Optional[CashMovementType]
    message: str
