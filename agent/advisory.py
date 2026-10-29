"""Conservative source hints for review, never a functional pass/fail oracle.

Only literal-width declarations and simple flat concatenations are recognized.
Unknown expressions, parameterized widths and ambiguous declarations are skipped.
No source is rewritten: truncation or extension may be intentional and must be
checked against the task. This needs no extra tool/model and uses no task IDs.
"""
from __future__ import annotations

from collections import defaultdict
import re

_NAME = r"[A-Za-z_$][A-Za-z0-9_$]*"
_RANGE = r"\[\s*(\d+)\s*:\s*(\d+)\s*\]"


def rtl_review_hints(source: str) -> list[str]:
    text = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"',
                  " ", source, flags=re.DOTALL)
    hints = []
    for module in re.finditer(rf"\bmodule\s+({_NAME})\b(.*?)\bendmodule\b",
                              text, re.DOTALL):
        body = module.group(2)
        declarations = defaultdict(list)
        for decl in re.finditer(
            rf"\b(?:input|output|inout|wire|reg|logic)\b"
            rf"(?:\s+(?:wire|reg|logic|signed|unsigned)\b)*\s*"
            rf"(?:{_RANGE}\s*)?({_NAME})(?=\s*[,;)=])", body,
        ):
            hi, lo, name = decl.groups()
            declarations[name].append(abs(int(hi) - int(lo)) + 1 if hi else 1)
        widths = {name: values[0] for name, values in declarations.items()
                  if len(values) == 1}

        def width(field: str) -> int | None:
            field = field.strip()
            literal = re.fullmatch(r"(\d+)\s*'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+", field)
            if literal:
                return int(literal.group(1))
            part = re.fullmatch(rf"({_NAME})\s*{_RANGE}", field)
            if part and part.group(1) in widths:
                return abs(int(part.group(2)) - int(part.group(3))) + 1
            bit = re.fullmatch(rf"({_NAME})\s*\[\s*\d+\s*\]", field)
            if bit and bit.group(1) in widths:
                return 1
            return widths.get(field)

        for assignment in re.finditer(
            rf"\b({_NAME})\s*(?:<=|=(?!=))\s*\{{([^{{}};]+)\}}\s*;", body,
        ):
            target, fields = assignment.groups()
            target_width = widths.get(target)
            field_widths = [width(field) for field in fields.split(",")]
            if target_width is None or any(w is None for w in field_widths):
                continue
            total = sum(field_widths)
            if total > target_width:
                hints.append(
                    f"{module.group(1)}.{target}: concatenation width {total} assigned "
                    f"to {target_width} bits discards {total - target_width} high bits. "
                    "Check the surviving fields against the task; intentional truncation is legal."
                )
            elif total < target_width:
                hints.append(
                    f"{module.group(1)}.{target}: concatenation width {total} assigned "
                    f"to {target_width} bits zero-extends {target_width - total} high bits. "
                    "Check for a single-bit select where a retained vector slice was intended; "
                    "intentional extension is legal."
                )
    return list(dict.fromkeys(hints))[:8]
