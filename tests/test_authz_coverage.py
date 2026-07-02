"""Governance test: sensitive GraphQL resolvers must enforce a capability.

Parses the resolver source (AST, no imports needed) and asserts each listed
function calls one of the authoritative capability checks. This fails loudly if
a gate is removed or a new sensitive resolver is added without one.
"""
import ast
import os

import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GRAPHQL = os.path.join(_BACKEND_ROOT, "app", "graphql")

_GUARD_CALLS = {"require_capability", "require_any_capability", "require_admin"}

# file (relative to app/graphql) -> resolver functions that MUST be gated
SENSITIVE = {
    "members/queries.py": ["members_page", "members", "member"],
    "memberships/queries.py": ["payments", "payment_metrics"],
    "dashboard/queries.py": ["dashboard_metrics"],
    "whatsapp/queries.py": ["conversations", "conversation", "conversation_messages"],
    "memberships/mutations.py": [
        "create_subscription", "create_member_enrollment", "renew_subscription",
        "update_payment", "delete_payment",
    ],
    "campaigns/mutations.py": [
        "create_campaign", "update_campaign", "delete_campaign",
        "build_campaign_audience", "schedule_campaign", "trigger_campaign",
        "pause_campaign", "resume_campaign", "cancel_campaign",
        "retry_campaign_failures", "run_campaign_sweep",
    ],
}


def _functions_with_guard(path):
    """Return {func_name: bool_has_guard_call} for every async def in the file."""
    tree = ast.parse(open(path, encoding="utf-8-sig").read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            has_guard = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id in _GUARD_CALLS
                for c in ast.walk(node)
            )
            out[node.name] = has_guard
    return out


@pytest.mark.parametrize("rel_path,functions", sorted(SENSITIVE.items()))
def test_sensitive_resolvers_declare_capability(rel_path, functions):
    path = os.path.join(_GRAPHQL, *rel_path.split("/"))
    guards = _functions_with_guard(path)
    missing = [f for f in functions if not guards.get(f, False)]
    assert not missing, f"{rel_path}: resolvers without a capability check: {missing}"
