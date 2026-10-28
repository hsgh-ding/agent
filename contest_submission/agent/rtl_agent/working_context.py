"""Bounded working memory. Complete dialogue, RTL and logs stay on disk."""
import re



def excerpt(text, limit=2400, *, diagnostics=False):
    lines = text.splitlines()
    selected = list(range(len(lines)))
    if diagnostics:
        selected = []
        for index, line in enumerate(lines):
            if re.search(r"error|fatal|failed|mismatch|timeout", line, re.I):
                selected.extend(range(max(0, index - 1), min(len(lines), index + 2)))
        selected = list(dict.fromkeys(selected)) or list(range(min(15, len(lines))))
    result, size, seen = [], 0, set()
    for index in selected:
        # Vivado duplicates diagnostics in several log files. Carry one copy.
        line = lines[index]
        if diagnostics and line in seen:
            continue
        seen.add(line)
        numbered = f"{index + 1}: {line}"
        remaining = limit - size
        if remaining <= 0:
            break
        result.append(numbered[:remaining])
        size += min(len(numbered), remaining) + 1
    return {"text": "\n".join(result), "total_lines": len(lines),
            "excerpt_only": len(result) < len(lines)}

