"""Governance test: booking flows that count capacity must hold the advisory lock.

The capacity check (count reservations vs session.capacity) has no backing DB
constraint, so any code path that counts-then-inserts MUST serialize on
``lock_class_session`` or two concurrent bookings can oversell a class (TOCTOU).
Parses the source with AST (no imports/DB needed) and fails loudly if the lock
call is removed.
"""
import ast
import os

import pytest

from app.crud.locks import _lock_key

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CRUD = os.path.join(_BACKEND_ROOT, "app", "crud")

# file (relative to app/crud) -> functions that MUST call lock_class_session
LOCKED_FUNCTIONS = {
    "memberships/day_passes.py": ["purchase_day_pass"],
    "reservationsCrud.py": ["create_reservation"],
    "standing_bookings/materialization.py": ["_create_reservation_if_possible"],
    # Taking over / giving back a regular class rewrites its reservations: same per-class lock.
    "special_events/takeover.py": ["apply_takeover", "apply_restore", "record_late_displacement"],
}

# file (relative to app/) -> functions that MUST call lock_special_event (seat holds, payment
# settlement and capacity changes for one event count-then-write like class bookings do).
EVENT_LOCKED_FUNCTIONS = {
    "crud/special_events/orders.py": ["create_hold", "add_manual_participant", "cancel_order"],
    "crud/special_events/events.py": [
        "update_event", "publish_event", "unpublish_event", "cancel_event",
    ],
    "crud/special_events/takeover.py": ["apply_takeover", "apply_restore"],
    "services/special_event_payments.py": ["_settle", "refund_order", "sweep_holds"],
}


def _functions_with_lock(path):
    tree = ast.parse(open(path, encoding="utf-8-sig").read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            has_lock = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "lock_class_session"
                for c in ast.walk(node)
            )
            out[node.name] = has_lock
    return out


@pytest.mark.parametrize("rel_path,functions", sorted(LOCKED_FUNCTIONS.items()))
def test_capacity_paths_take_session_lock(rel_path, functions):
    path = os.path.join(_CRUD, *rel_path.split("/"))
    locked = _functions_with_lock(path)
    missing = [f for f in functions if not locked.get(f, False)]
    assert not missing, f"{rel_path}: capacity paths without lock_class_session: {missing}"


@pytest.mark.parametrize("rel_path,functions", sorted(EVENT_LOCKED_FUNCTIONS.items()))
def test_special_event_paths_take_event_lock(rel_path, functions):
    path = os.path.join(_BACKEND_ROOT, "app", *rel_path.split("/"))
    calling = _functions_calling(path, "lock_special_event")
    missing = [f for f in functions if not calling.get(f, False)]
    assert not missing, f"{rel_path}: event paths without lock_special_event: {missing}"


# Multi-session batch entry points MUST take the coarse batch lock to avoid an
# ABBA deadlock between concurrent batches (each accumulates many per-session
# locks in a data-dependent order held until the terminal commit).
BATCH_FUNCTIONS = {
    "pos/sales.py": ["create_sale"],
    "standing_bookings/materialization.py": ["materialize_standing_bookings"],
    "standing_bookings/reschedule.py": ["reschedule_standing_booking"],
}


def _functions_calling(path, callee):
    tree = ast.parse(open(path, encoding="utf-8-sig").read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            out[node.name] = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == callee
                for c in ast.walk(node)
            )
    return out


@pytest.mark.parametrize("rel_path,functions", sorted(BATCH_FUNCTIONS.items()))
def test_batch_paths_take_batch_lock(rel_path, functions):
    path = os.path.join(_CRUD, *rel_path.split("/"))
    calling = _functions_calling(path, "lock_materialization_batch")
    missing = [f for f in functions if not calling.get(f, False)]
    assert not missing, f"{rel_path}: batch paths without lock_materialization_batch: {missing}"


def test_lock_key_is_stable_and_64bit():
    """The advisory-lock key must be deterministic across processes and fit int8."""
    key = _lock_key("class_session", 42)
    assert key == _lock_key("class_session", 42)
    assert -(2**63) <= key < 2**63
    # namespacing: same id under another namespace must not collide
    assert key != _lock_key("other_ns", 42)
    assert _lock_key("class_session", 1) != _lock_key("class_session", 2)
