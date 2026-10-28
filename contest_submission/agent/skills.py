"""Skill pack loading and selection.

Follows the format of AMD's official Vitis HLS agent skills, so a skill from
that pack can be dropped into skill/ and used unchanged:

    ---
    name: hls-flattenable
    description: one line, no block scalar
    ---

    <!-- copyright -->

    # Agent Skill: ...

    ## Skill Metadata
    - **Name:** ...
    - **Description:** ...
    - **Trigger:** prose, when a human would reach for this
    - **Log signatures:** `HLS 214-157`, `undefined symbol`      <- see below

Front matter carries `name` and `description` only. Everything else lives in the
body, which is why the loader reads the Skill Metadata bullets.

`**Log signatures:**` is an addition, not part of the official format. Official
skills are selected by an agent that has read their descriptions; this agent
selects by matching tool output, and prose triggers ("User asks whether loops are
flattenable") do not appear in a synthesis log. Backticked items on that line are
matched literally against the log. A skill without the line is still loaded and
can still be selected by name -- it just never auto-fires on an error.

Selection is deliberately dumb. A model-driven router would be more flexible but
costs a round trip, and a wrong route is expensive under a wall clock budget.
Start dumb; earn the complexity.

Injecting every skill every round is the failure mode to avoid. Skill bodies run
to thousands of tokens; five of them crowd out the error log they are meant to
help interpret.
"""

from __future__ import annotations

import os
import re


class Skill:
    def __init__(self, name: str, description: str, signatures: list[str],
                 trigger: str, body: str, path: str):
        self.name = name
        self.description = description
        self.signatures = signatures
        self.trigger = trigger
        self.body = body
        self.path = path

    def matches(self, *texts: str) -> bool:
        if not self.signatures:
            return False
        haystack = "\n".join(t.lower() for t in texts if t)
        return any(sig.lower() in haystack for sig in self.signatures)

    def render(self, budget: int = 12000) -> str:
        return f'<skill name="{self.name}">\n{_trim(self.body, budget)}\n</skill>'


def load_skills(skill_dir: str) -> list[Skill]:
    skills: list[Skill] = []
    if not os.path.isdir(skill_dir):
        return skills

    for entry in sorted(os.listdir(skill_dir)):
        path = os.path.join(skill_dir, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            continue

        meta, body = _split_front_matter(raw)
        skills.append(
            Skill(
                name=meta.get("name", entry),
                description=meta.get("description", ""),
                signatures=_bullet_code_items(body, "Log signatures"),
                trigger=_bullet_text(body, "Trigger"),
                body=body,
                path=path,
            )
        )
    return skills


def select(skills: list[Skill], *context: str, limit: int = 2) -> list[Skill]:
    return [s for s in skills if s.matches(*context)][:limit]


# ------------------------------------------------------------- truncation

def _trim(body: str, budget: int) -> str:
    """Drop whole trailing sections rather than cutting mid-sentence.

    Cutting at a character offset is worse than it looks. A skill's last two
    sections are usually the output format and the behavioural constraints --
    exactly the parts that keep a model from freelancing. Losing them silently
    turns a precise skill into vague advice.

    So: cut at a `## ` boundary, and say out loud which sections went.
    """
    if len(body) <= budget:
        return body

    sections = re.split(r"(?=^## )", body, flags=re.MULTILINE)
    kept: list[str] = []
    used = 0
    for sec in sections:
        if used + len(sec) > budget and kept:
            break
        kept.append(sec)
        used += len(sec)

    dropped = [
        s.splitlines()[0].lstrip("# ").strip()
        for s in sections[len(kept):] if s.strip()
    ]
    note = f"\n\n... [skill truncated, sections omitted: {', '.join(dropped)}]"
    return "".join(kept).rstrip() + note


# --------------------------------------------------------- Skill Metadata

def _bullet_code_items(body: str, label: str) -> list[str]:
    """Backticked items from a `- **<label>:** \\`a\\`, \\`b\\`` bullet."""
    line = _bullet_text(body, label)
    return re.findall(r"`([^`]+)`", line)


def _bullet_text(body: str, label: str) -> str:
    m = re.search(rf"^\s*[-*]\s*\*\*{re.escape(label)}:?\*\*\s*(.+)$",
                  body, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------ front matter

def _split_front_matter(raw: str) -> tuple[dict, str]:
    """Parse the two scalar keys the official format uses.

    A real YAML parser would be one more dependency to bake into the image for
    two fields. If your skills need richer front matter, add pyyaml -- but add it
    to requirements.txt, because the sandbox cannot download it.
    """
    if not raw.lstrip().startswith("---"):
        return {}, raw.strip()

    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.DOTALL)
    if not m:
        return {}, raw.strip()

    head, body = m.group(1), m.group(2)
    meta: dict = {}
    key: str | None = None
    block: list[str] = []

    for line in head.splitlines():
        if not line.strip():
            continue
        # Continuation of a folded/literal block, or a wrapped value.
        if line[0] in " \t" and key:
            block.append(line.strip())
            continue

        if key and block:
            meta[key] = " ".join(block).strip()
            block = []
        key = None

        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v in ("|", ">", "|-", ">-", ""):
            key = k                       # value continues on indented lines
        else:
            meta[k] = v.strip("\"'")

    if key and block:
        meta[key] = " ".join(block).strip()

    return meta, body.strip()
