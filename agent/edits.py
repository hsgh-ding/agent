"""Apply bounded, exact-anchor edits to the current in-memory RTL.

Model output is data, never Python, a shell command, or a filesystem path.
Every search must be nonempty and unique in the current candidate. All edits
are validated before the caller can save or verify the new source.
"""
from __future__ import annotations

import json
import re


class EditError(ValueError):
    pass


def decode_edit_reply(reply: str, current: str, *, allow_keep: bool = False
                      ) -> tuple[str, str, int] | None:
    """Return (source, mode, edit_count), or None for a regular RTL reply."""
    text = reply.strip()
    # Models sometimes label edit JSON as "verilog". The JSON object shape,
    # not an optional Markdown language label, determines the reply type.
    fence = re.fullmatch(r"```(?:[A-Za-z0-9_-]+)?\s*\n(.*?)\n```", text, re.DOTALL | re.I)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise EditError("Invalid edit JSON; return valid JSON or the complete RTL.") from exc
    if not isinstance(data, dict):
        raise EditError("Edit response must be a JSON object.")
    if data.get("decision") == "keep":
        if not allow_keep or not current:
            raise EditError("KEEP is allowed only in a review of a checked candidate, not after a tool failure.")
        if set(data) - {"decision", "reason"}:
            raise EditError("KEEP cannot be combined with edits or other fields.")
        return current, "keep", 0
    if set(data) - {"edits", "reason"}:
        raise EditError("Use only edits (and optional reason); paths and executable commands are not accepted.")
    edits = data.get("edits")
    if not current or not isinstance(edits, list) or not 1 <= len(edits) <= 16:
        raise EditError("Supply 1..16 edits for the current RTL, or return the full RTL.")
    result = current
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or set(edit) != {"search", "replace"}:
            raise EditError(f"Edit {index}: exactly search and replace are required.")
        search, replacement = edit["search"], edit["replace"]
        if not isinstance(search, str) or not search or not isinstance(replacement, str):
            raise EditError(f"Edit {index}: search must be nonempty text; replace must be text.")
        matches = result.count(search)
        if matches != 1:
            raise EditError(f"Edit {index}: search occurs {matches} times; provide a unique exact anchor.")
        result = result.replace(search, replacement, 1)
    return result, "edits", len(edits)
