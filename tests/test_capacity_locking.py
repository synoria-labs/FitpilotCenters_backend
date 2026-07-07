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
    "reservationsCrud.py": ["create_reservation"],
    "standing_bookings/materialization.py": ["_create_reservation_if_possible"],
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


def test_lock_key_is_stable_and_64bit():
    """The advisory-lock key must be deterministic across processes and fit int8."""
    key = _lock_key("class_session", 42)
    assert key == _lock_key("class_session", 42)
    assert -(2**63) <= key < 2**63
    # namespacing: same id under another namespace must not collide
    assert key != _lock_key("other_ns", 42)
    assert _lock_key("class_session", 1) != _lock_key("class_session", 2)
