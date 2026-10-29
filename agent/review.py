"""Validate source-grounded reviewer reports, not functional correctness.

The model's expected/observed values remain claims: no simulator or oracle is
hidden in this module. Quotes must exist in this task and this source before a
claim may trigger repair. A report itself never edits RTL or executes code.
"""
from __future__ import annotations

import json
import re

from .llm import strip_think


class ReviewError(ValueError):
    pass


def _normalized(text: str) -> str:
    return " ".join(text.split())


def parse_review(reply: str, task: str, source: str) -> list[dict[str, str]]:
    text = strip_think(reply).strip()
    fence = re.fullmatch(r"```(?:[A-Za-z0-9_-]+)?\s*\n(.*?)\n```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReviewError("Return one JSON object with checks; do not return RTL or a bare KEEP.") from exc
    if not isinstance(data, dict) or set(data) != {"checks"}:
        raise ReviewError("Review must contain only checks; edits, commands and bare KEEP are not review evidence.")
    checks = data["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 6:
        raise ReviewError("Provide 1..6 concrete checks against the task.")
    fields = {"requirement", "source", "case", "expected", "observed", "status"}
    seen = set()
    for index, check in enumerate(checks, 1):
        if (not isinstance(check, dict) or set(check) != fields
                or any(not isinstance(value, str) or not value.strip() or len(value) > 1200
                       for value in check.values())):
            raise ReviewError(f"Check {index}: use nonempty requirement/source/case/expected/observed/status strings.")
        if _normalized(check["requirement"]) not in _normalized(task):
            raise ReviewError(f"Check {index}: requirement must quote the actual task/interface verbatim.")
        if _normalized(check["source"]) not in _normalized(source):
            raise ReviewError(f"Check {index}: source must quote the current candidate verbatim.")
        status = check["status"]
        if status not in {"match", "mismatch", "uncertain"}:
            raise ReviewError(f"Check {index}: status must be match, mismatch or uncertain.")
        equal = _normalized(check["expected"]) == _normalized(check["observed"])
        if (status == "match" and not equal) or (status == "mismatch" and equal):
            raise ReviewError(f"Check {index}: use comparable values consistent with the status.")
        key = tuple(_normalized(check[name]) for name in ("requirement", "source", "case"))
        if key in seen:
            raise ReviewError(f"Check {index}: duplicate check; inspect a different boundary or behavior.")
        seen.add(key)
    return checks
