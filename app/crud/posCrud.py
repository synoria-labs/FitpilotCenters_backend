"""Aggregated POS CRUD facade (mirrors app.crud.membershipsCrud)."""
from app.crud.pos.cash_sessions import (
    close_cash_session,
    compute_expected_cash,
    get_cash_session_by_id,
    get_open_cash_session,
    open_cash_session,
    record_cash_movement,
)
from app.crud.pos.reports import (
    CashSessionReportData,
    MethodTotalData,
    get_cash_session_report,
)
from app.crud.pos.products import (
    adjust_stock,
    create_product,
    get_product_by_id,
    get_products,
    set_product_active,
    update_product,
)
from app.crud.pos.sales import (
    SaleLineInput,
    SalePaymentInput,
    create_sale,
    get_sale,
    get_sales,
    void_sale,
)

__all__ = [
    "get_products",
    "get_product_by_id",
    "create_product",
    "update_product",
    "set_product_active",
    "adjust_stock",
    "open_cash_session",
    "close_cash_session",
    "record_cash_movement",
    "get_open_cash_session",
    "get_cash_session_by_id",
    "compute_expected_cash",
    "get_cash_session_report",
    "CashSessionReportData",
    "MethodTotalData",
    "create_sale",
    "get_sale",
    "get_sales",
    "void_sale",
    "SaleLineInput",
    "SalePaymentInput",
]
