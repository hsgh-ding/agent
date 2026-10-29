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

from .retrieval import routing_text


class Skill:
    def __init__(self, name: str, description: str, signatures: list[str],
                 trigger: str, signatures_keywords: list[str], body: str,
                 path: str, always: bool = False):
        self.name = name
        self.description = description
        self.signatures = signatures
        self.trigger = trigger
        self.keywords = signatures_keywords
        self.body = body
        self.path = path
        self.always = always

    def matches(self, *texts: str) -> bool:
        if self.always:
            return True
        if not self.signatures and not self.keywords:
            return False
        haystack = "\n".join(t.lower() for t in texts if t)
        return (any(sig.lower() in haystack for sig in self.signatures)
                or any(word.lower() in haystack for word in self.keywords))

    def render(self, budget: int = 1800, *, context: str = "") -> str:
        # Import reference rules, not another persona, sample interface or
        # conflicting standalone analysis output format.
        sections = re.split(r"(?=^## )", self.body, flags=re.MULTILINE)
        runtime = [s for s in sections if s.startswith("## Runtime guidance\n")]
        headings = {
            "## Rules", "## Workflow", "## Common Vivado diagnostics",
        }
        if re.search(r"\b(?:sequence|detector|pattern|overlap)\b|序列|重叠", context, re.I):
            headings.add("## Sequence detector timing")
        rules = [s for s in sections if s.strip() and s.splitlines()[0].strip() in headings]
        body = "\n".join(runtime or rules) if runtime or rules else self.body
        return f'<reference name="{self.name}">\n{_trim(body, budget)}\n</reference>'


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
                signatures_keywords=_bullet_code_items(body, "Keywords"),
                body=body,
                path=path,
                always=meta.get("always", "").lower() == "true",
            )
        )
    return skills


def select(skills: list[Skill], *context: str, limit: int = 2) -> list[Skill]:
    """Select matching error skills without loading unrelated material.

    Interface-contract guidance is useful on the first generation as well as on
    repairs, so a skill can opt in with ``Always: true`` in its metadata.
    Keywords can match the task statement; log signatures target repair rounds.
    """
    task = routing_text(context[0]).lower() if context else ""
    log = "\n".join(text.lower() for text in context[1:] if text)
    generic = {"module", "interface", "port", "input", "output", "bits",
               "wire", "reg", "vivado", "synthesis", "elaboration"}

    def keyword_match(word: str) -> bool:
        word = word.lower()
        if word in generic:
            return False
        if word.isascii():
            return bool(re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z_])",
                                  task + "\n" + log))
        return word in task + log

    def rank(skill: Skill) -> int:
        signature_hits = sum(sig.lower() in log for sig in skill.signatures)
        keyword_hits = sum(keyword_match(word) for word in skill.keywords)
        return signature_hits * 100 + keyword_hits * 10

    defaults = sorted((s for s in skills if s.always), key=lambda s: s.name)[:1]
    ranked = [(rank(skill), skill) for skill in skills if not skill.always]
    ranked = [(score, skill) for score, skill in ranked if score > 0]
    ranked.sort(key=lambda pair: (-pair[0], pair[1].name))
    return (defaults + [skill for _, skill in ranked])[:max(0, limit)]


# ------------------------------------------------------------- truncation

def _trim(body: str, budget: int) -> str:
    """Bound runtime reference size and mark any shortening explicitly."""
    if len(body) <= budget:
        return body

    # Also cap an oversized first section; preserve whole lines.
    notice = "\n... [reference shortened]"
    cut = body[:max(0, budget - len(notice))]
    return cut.rsplit("\n", 1)[0] + notice


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
