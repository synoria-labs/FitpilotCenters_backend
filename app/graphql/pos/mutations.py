import logging

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.permissions import MANAGE_CASH_SESSION, MANAGE_PRODUCTS, OPERATE_POS
from app.crud.posCrud import (
    SaleLineInput,
    SalePaymentInput,
    close_cash_session,
    create_product,
    create_sale,
    open_cash_session,
    record_cash_movement,
    set_product_active,
    update_product,
    void_sale,
)
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.pos.types import (
    CashMovementInput,
    CashMovementResponse,
    CashMovementType,
    CashSessionResponse,
    CashSessionType,
    CloseCashSessionInput,
    CreateProductInput,
    CreateSaleInput,
    OpenCashSessionInput,
    ProductMutationResponse,
    ProductType,
    SaleResponse,
    SaleType,
    UpdateProductInput,
)

logger = logging.getLogger(__name__)


@strawberry.type
class PosMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def open_cash_session(self, info: Info, input: OpenCashSessionInput) -> CashSessionResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, MANAGE_CASH_SESSION)
        if error:
            return CashSessionResponse(success=False, session=None, message=error)
        try:
            account_id = getattr(info.context, "account_id", None)
            session = await open_cash_session(
                db,
                opened_by=account_id,
                opening_float=input.opening_float,
                notes=input.notes,
            )
            return CashSessionResponse(
                success=True,
                session=CashSessionType.from_model(session),
                message="Caja abierta exitosamente.",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return CashSessionResponse(success=False, session=None, message=str(e))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def close_cash_session(self, info: Info, input: CloseCashSessionInput) -> CashSessionResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, MANAGE_CASH_SESSION)
        if error:
            return CashSessionResponse(success=False, session=None, message=error)
        try:
            account_id = getattr(info.context, "account_id", None)
            session = await close_cash_session(
                db,
                cash_session_id=input.cash_session_id,
                counted_cash=input.counted_cash,
                closed_by=account_id,
                notes=input.notes,
            )
            return CashSessionResponse(
                success=True,
                session=CashSessionType.from_model(session),
                message="Corte de caja realizado exitosamente.",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return CashSessionResponse(success=False, session=None, message=str(e))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def record_cash_movement(self, info: Info, input: CashMovementInput) -> CashMovementResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, MANAGE_CASH_SESSION)
        if error:
            return CashMovementResponse(success=False, movement=None, message=error)
        try:
            account_id = getattr(info.context, "account_id", None)
            movement = await record_cash_movement(
                db,
                cash_session_id=input.cash_session_id,
                direction=input.direction,
                amount=input.amount,
                reason=input.reason,
                created_by=account_id,
            )
            return CashMovementResponse(
                success=True,
                movement=CashMovementType.from_model(movement),
                message="Movimiento registrado exitosamente.",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return CashMovementResponse(success=False, movement=None, message=str(e))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_sale(self, info: Info, input: CreateSaleInput) -> SaleResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, OPERATE_POS)
        if error:
            return SaleResponse(success=False, sale=None, message=error)
        try:
            account_id = getattr(info.context, "account_id", None)
            line_items = [
                SaleLineInput(
                    line_type=li.line_type,
                    description=li.description or "",
                    quantity=li.quantity or 1,
                    unit_price=li.unit_price,
                    discount=li.discount or 0,
                    plan_id=li.plan_id,
                    member_id=li.member_id,
                    full_name=li.full_name,
                    email=li.email,
                    phone_number=li.phone_number,
                    start_at=li.start_at,
                    template_id=li.template_id,
                    seat_id=li.seat_id,
                    product_id=li.product_id,
                )
                for li in input.line_items
            ]
            tenders = [
                SalePaymentInput(
                    method=p.method,
                    amount=p.amount,
                    provider=p.provider,
                    provider_payment_id=p.provider_payment_id,
                    external_reference=p.external_reference,
                )
                for p in input.payments
            ]
            sale = await create_sale(
                db,
                line_items=line_items,
                tenders=tenders,
                person_id=input.person_id,
                sold_by=account_id,
                note=input.note,
            )
            return SaleResponse(
                success=True,
                sale=SaleType.from_model(sale),
                message="Venta registrada exitosamente.",
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error creating sale: %s", e, exc_info=True)
            await db.rollback()
            return SaleResponse(success=False, sale=None, message=str(e))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_product(self, info: Info, input: CreateProductInput) -> ProductMutationResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, MANAGE_PRODUCTS)
        if error:
            return ProductMutationResponse(success=False, product=None, message=error)
        try:
            product = await create_product(
                db,
                name=input.name,
                price=input.price,
                sku=input.sku,
                track_stock=input.track_stock,
                stock_qty=input.stock_qty,
                is_active=input.is_active,
            )
            return ProductMutationResponse(
                success=True,
                product=ProductType.from_model(product),
                message="Producto creado exitosamente.",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return ProductMutationResponse(success=False, product=None, message=str(e))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_product(self, info: Info, input: UpdateProductInput) -> ProductMutationResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, MANAGE_PRODUCTS)
        if error:
            return ProductMutationResponse(success=False, product=None, message=error)

        _UNSET = object()

        def _opt(value):
            return value if value is not None else _UNSET

        try:
            product = await update_product(
                db,
                input.product_id,
                name=_opt(input.name),
                price=_opt(input.price),
                sku=_opt(input.sku),
                track_stock=_opt(input.track_stock),
                stock_qty=_opt(input.stock_qty),
                is_active=_opt(input.is_active),
            )
            if product is None:
                return ProductMutationResponse(success=False, product=None, message="Producto no encontrado.")
            return ProductMutationResponse(
                success=True,
                product=ProductType.from_model(product),
                message="Producto actualizado exitosamente.",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return ProductMutationResponse(success=False, product=None, message=str(e))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def set_product_active(self, info: Info, product_id: int, is_active: bool) -> ProductMutationResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, MANAGE_PRODUCTS)
        if error:
            return ProductMutationResponse(success=False, product=None, message=error)
        try:
            product = await set_product_active(db, product_id, is_active)
            if product is None:
                return ProductMutationResponse(success=False, product=None, message="Producto no encontrado.")
            action = "reactivado" if is_active else "desactivado"
            return ProductMutationResponse(
                success=True,
                product=ProductType.from_model(product),
                message=f"Producto {action} exitosamente.",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return ProductMutationResponse(success=False, product=None, message=str(e))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def void_sale(self, info: Info, sale_id: int) -> SaleResponse:
        db: AsyncSession = info.context.db
        error = await require_capability(info, OPERATE_POS)
        if error:
            return SaleResponse(success=False, sale=None, message=error)
        try:
            sale = await void_sale(db, sale_id)
            if sale is None:
                return SaleResponse(success=False, sale=None, message="Venta no encontrada.")
            return SaleResponse(
                success=True,
                sale=SaleType.from_model(sale),
                message="Venta anulada.",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return SaleResponse(success=False, sale=None, message=str(e))
