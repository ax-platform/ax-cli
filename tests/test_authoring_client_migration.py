"""Regression: structural invariants of the get_authoring_client() migration.

Issue #142 Phase 3 introduces ``get_authoring_client()`` as the single entry
point for resolving a credential-bearing AxClient, and migrates the
~67 ``get_client()`` call sites across 19 command modules to use it.

This module pins the structural side of that migration (the behavioral
side is in ``tests/test_authoring_client.py``). It exists so that a future
refactor or a new command landing on ``main`` cannot silently regress the
contract by reaching for the legacy resolver.

What is locked
==============

  - ``ax_cli.config.get_authoring_client`` is the canonical factory and
    is callable.
  - ``ax_cli.config.get_client`` is preserved for backwards compatibility
    (the issue explicitly required not removing it).
  - Every migrated command module imports and uses
    ``get_authoring_client``; none of them call ``get_client()`` directly.
  - ``commands/gateway.py`` continues to expose the two helpers that
    ``get_authoring_client()`` lazy-imports
    (``_load_gateway_user_client`` and ``_load_managed_agent_client``).
    Renaming or removing them would break the factory at runtime.

What is intentionally NOT locked here
=====================================

Jake's smoke test 5 in #142 also lists ``_load_gateway_user_client``,
``_load_managed_agent_client``, ``_gateway_local_call``, and
``AxClient(...)`` as patterns that should disappear from migrated
modules. Phase 3 leaves several of those in place on purpose:

  - ``commands/gateway.py`` defines and legitimately uses the two
    ``_load_*`` helpers for bootstrap-user and explicit named-agent
    operations (``ax gateway login``, ``ax gateway agents add``,
    ``ax gateway agents send <name>``). These resolve identities other
    than the invoking principal and are out of scope for the factory.
  - ``commands/auth.py`` retains four ``AxClient(...)`` constructions
    plus one ``_gateway_local_call(method="whoami")``. They probe
    explicit credentials (``whoami`` against a candidate token), which
    is fundamentally a different operation from "give me a client for
    the invoking principal."
  - ``commands/qa.py`` retains one ``AxClient(...)`` for the QA
    test-environment auth flow with explicit credentials.
  - ``commands/messages.py`` defines ``_gateway_local_call`` itself and
    is its primary caller. ``tasks.py``, ``agents.py``, ``context.py``,
    and ``spaces.py`` still dispatch through it inside their
    ``if gateway_cfg:`` branches.

Collapsing the ``if gateway_cfg: _gateway_local_call(...) else:
client.method(...)`` branches into a single
``client = get_authoring_client(); client.method(...)`` is restructural
(it removes the daemon-proxy intermediate hop and changes wire-level
behavior in brokered shells) and lands in a follow-up PR. The pin in
this file is intentionally narrower than Jake's smoke test 5 so the
deferred work is visible rather than hidden.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ax_cli import config as ax_config

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = REPO_ROOT / "ax_cli" / "commands"

# The 19 modules from #142's "What to migrate" list, minus
# ``commands/gateway.py``. See module docstring for why gateway.py is
# excluded — it implements the gateway command suite itself and uses
# explicit-identity helpers that resolve identities other than the
# invoking principal.
MIGRATED_MODULES = (
    "agents",
    "alerts",
    "apps",
    "auth",
    "channel",
    "context",
    "credentials",
    "events",
    "handoff",
    "heartbeat",
    "keys",
    "listen",
    "messages",
    "qa",
    "reminders",
    "spaces",
    "tasks",
    "upload",
    "watch",
)


def _module_path(name: str) -> Path:
    return COMMANDS_DIR / f"{name}.py"


def _parse(name: str) -> ast.AST:
    return ast.parse(_module_path(name).read_text(encoding="utf-8"))


def _direct_calls(tree: ast.AST, func_name: str) -> list[int]:
    """Return line numbers of every ``func_name()`` call in ``tree``
    where ``func_name`` is a module-level name (i.e. not an attribute
    access like ``ax_config.get_client()``).

    Catches the migration target — ``client = get_client()`` after
    ``from ..config import get_client``. Misses qualified calls on
    purpose; those are part of test infrastructure (e.g.
    ``test_authoring_client.py``'s backwards-compat test).
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == func_name:
            hits.append(node.lineno)
    return hits


# ---- Test 1: factory is exported and callable -------------------------------


def test_get_authoring_client_is_exported_and_callable() -> None:
    """The factory must live on ``ax_cli.config`` so command modules can
    import it from the public surface."""
    assert hasattr(ax_config, "get_authoring_client"), (
        "ax_cli.config.get_authoring_client is missing. The migration in "
        "#142 introduced this as the single entry point for credential "
        "resolution; every migrated module imports it."
    )
    assert callable(ax_config.get_authoring_client)


# ---- Test 2: get_client preserved for backwards compatibility ---------------


def test_get_client_preserved_in_config() -> None:
    """Issue #142 explicitly required ``get_client`` not be renamed or
    removed. Legacy callers (and ``get_authoring_client`` itself, on the
    fallback path) still reach for it."""
    assert hasattr(ax_config, "get_client"), (
        "ax_cli.config.get_client was removed. The issue requires it stay "
        "as a public function for backwards compatibility — get_authoring_"
        "client falls through to it on the non-brokered path."
    )
    assert callable(ax_config.get_client)


# ---- Test 3: no migrated module calls get_client() directly -----------------


@pytest.mark.parametrize("module_name", MIGRATED_MODULES)
def test_migrated_module_does_not_call_get_client_directly(module_name: str) -> None:
    """Hard contract: every credential-resolution call site in a migrated
    module must go through ``get_authoring_client()``.

    Catches the easy regression where a new command lands on main with
    ``client = get_client()`` and silently breaks in Gateway-brokered
    shells (which is the exact failure mode that motivated #142).
    """
    tree = _parse(module_name)
    hits = _direct_calls(tree, "get_client")
    assert not hits, (
        f"ax_cli/commands/{module_name}.py calls get_client() directly at "
        f"line(s) {hits}. Use get_authoring_client() instead so the call "
        f"resolves correctly in Gateway-brokered shells."
    )


# ---- Test 4: every migrated module uses get_authoring_client() --------------


@pytest.mark.parametrize("module_name", MIGRATED_MODULES)
def test_migrated_module_uses_get_authoring_client(module_name: str) -> None:
    """Positive lock: each migrated module must contain at least one
    call to ``get_authoring_client()``.

    Without this, a future refactor could quietly remove the factory
    call from a module entirely (e.g. by inlining a different resolver)
    and the negative test above wouldn't catch it.
    """
    tree = _parse(module_name)
    hits = _direct_calls(tree, "get_authoring_client")
    assert hits, (
        f"ax_cli/commands/{module_name}.py does not call "
        f"get_authoring_client(). Every migrated module is expected to "
        f"resolve its CLI client through this factory."
    )


# ---- Test 5: gateway.py keeps the lazy-import targets alive -----------------


def test_gateway_command_module_exposes_lazy_import_targets() -> None:
    """``get_authoring_client()`` lazy-imports two helpers from
    ``ax_cli.commands.gateway`` to break the otherwise-circular module
    graph. If either symbol is renamed or removed, the factory raises
    at first call instead of at import time, which is much harder to
    debug.

    Lock the names at the module's top-level surface.
    """
    from ax_cli.commands import gateway as gateway_cmd

    assert hasattr(gateway_cmd, "_load_gateway_user_client"), (
        "ax_cli.commands.gateway._load_gateway_user_client is missing. "
        "get_authoring_client() reaches for it via lazy import to load "
        "the bootstrap-user client; renaming it breaks brokered shells."
    )
    assert hasattr(gateway_cmd, "_load_managed_agent_client"), (
        "ax_cli.commands.gateway._load_managed_agent_client is missing. "
        "get_authoring_client() reaches for it via lazy import to load "
        "a managed-agent client; renaming it breaks brokered shells."
    )
    assert callable(gateway_cmd._load_gateway_user_client)
    assert callable(gateway_cmd._load_managed_agent_client)
