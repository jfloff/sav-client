"""Authorization metadata loader for sav-mcp tool definitions.

Reads the per-tool policy from ``authz.toml``, validates it against a fixed
role vocabulary, and stamps every registered FastMCP tool's ``_meta`` plus the
relevant ``inputSchema`` properties with the ``x-sav-*`` extension fields
documented in AGENTS.md.

The trust boundary lives at the downstream wrapper (e.g. the gedai-bot
Telegram frontend); sav-mcp does **not** enforce these decisions. The file
only *describes* them so the wrapper can filter the exposed catalog and
verify subject ownership before forwarding each call.

Downstream consumers may ``from sav_mcp.authz import load_policy, ToolAuthz``
to read the same TOML directly — there is one source of truth.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

Capability = Literal["read", "write", "delete"]
SubjectKind = Literal["license", "nif"]

_ALLOWED_CAPABILITIES: frozenset[str] = frozenset(get_args(Capability))
_ALLOWED_SUBJECT_KINDS: frozenset[str] = frozenset(get_args(SubjectKind))


@dataclass(frozen=True)
class ToolAuthz:
    """Per-tool authorization policy, materialised from one ``[tools.X]`` block."""

    capability: Capability
    roles: tuple[str, ...]
    self_scope: tuple[str, ...]
    identity_params: tuple[str, ...]
    subject_license: tuple[str, ...]
    subject_nif: tuple[str, ...]


_KNOWN_TOOL_KEYS: frozenset[str] = frozenset({
    "capability", "roles", "self_scope",
    "identity_params", "subject_license", "subject_nif",
})


def load_policy(
    path: Path,
) -> tuple[dict[str, ToolAuthz], frozenset[str]]:
    """Read and validate ``authz.toml``.

    Returns ``(policy, allowed_roles)``. Raises ``ValueError`` on any
    vocabulary violation, unknown field, or missing required key — drift
    is caught at import time, never at request time.
    """
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults: dict = raw.get("defaults", {})
    allowed_roles = frozenset(raw.get("roles", {}).get("allowed", ()))
    if not allowed_roles:
        raise ValueError(f"{path.name}: [roles].allowed must list ≥1 role")

    tools_raw: dict = raw.get("tools", {})
    policy: dict[str, ToolAuthz] = {}
    for name, entry in tools_raw.items():
        unknown = set(entry) - _KNOWN_TOOL_KEYS
        if unknown:
            raise ValueError(
                f"{path.name}: tool {name!r} has unknown keys {sorted(unknown)}; "
                f"allowed: {sorted(_KNOWN_TOOL_KEYS)}"
            )
        merged = {**defaults, **entry}
        cap = merged.get("capability", "read")
        if cap not in _ALLOWED_CAPABILITIES:
            raise ValueError(
                f"{path.name}: tool {name!r} has invalid capability {cap!r}; "
                f"must be one of {sorted(_ALLOWED_CAPABILITIES)}"
            )
        roles = tuple(merged.get("roles", ()))
        self_scope = tuple(merged.get("self_scope", ()))
        for r in roles:
            if r not in allowed_roles:
                raise ValueError(
                    f"{path.name}: tool {name!r} lists role {r!r} not in vocabulary"
                )
        for r in self_scope:
            if r == "coach":
                raise ValueError(
                    f"{path.name}: tool {name!r} cannot self_scope 'coach'; "
                    "coaches operate on others' subjects by definition"
                )
            if r not in allowed_roles:
                raise ValueError(
                    f"{path.name}: tool {name!r} lists self_scope role "
                    f"{r!r} not in vocabulary"
                )
        policy[name] = ToolAuthz(
            capability=cap,
            roles=roles,
            self_scope=self_scope,
            identity_params=tuple(merged.get("identity_params", ())),
            subject_license=tuple(merged.get("subject_license", ())),
            subject_nif=tuple(merged.get("subject_nif", ())),
        )
    return policy, allowed_roles


def apply_to_server(
    server: "FastMCP",
    policy: dict[str, ToolAuthz],
) -> None:
    """Stamp every registered FastMCP tool with its ``x-sav-*`` metadata.

    Raises ``RuntimeError`` if the registry and the policy drift apart so new
    tools cannot ship without an explicit authorization decision.
    """
    registered = {t.name for t in server._tool_manager.list_tools()}
    declared = set(policy)
    missing = registered - declared
    if missing:
        raise RuntimeError(
            f"Tools registered without authz.toml entries: {sorted(missing)}. "
            "Every MCP tool MUST carry a [tools.<name>] block."
        )
    stale = declared - registered
    if stale:
        raise RuntimeError(
            f"authz.toml has entries for unregistered tools: {sorted(stale)}."
        )
    for name, authz in policy.items():
        tool = server._tool_manager.get_tool(name)
        assert tool is not None  # guaranteed by the missing/stale checks
        meta = dict(tool.meta) if tool.meta else {}
        meta["x-sav-capability"] = authz.capability
        meta["x-sav-roles"] = list(authz.roles)
        if authz.self_scope:
            meta["x-sav-self-scope"] = list(authz.self_scope)
        tool.meta = meta
        properties = tool.parameters.get("properties", {})
        for param in authz.identity_params:
            _require_param(name, properties, param, "identity_params")
            properties[param]["x-sav-identity"] = True
        for param in authz.subject_license:
            _require_param(name, properties, param, "subject_license")
            properties[param]["x-sav-subject"] = "license"
        for param in authz.subject_nif:
            _require_param(name, properties, param, "subject_nif")
            properties[param]["x-sav-subject"] = "nif"


def _require_param(
    tool_name: str,
    properties: dict,
    param: str,
    field_name: str,
) -> None:
    if param not in properties:
        raise RuntimeError(
            f"Tool {tool_name!r}: {field_name} lists {param!r} which is not in "
            "the tool's inputSchema."
        )
