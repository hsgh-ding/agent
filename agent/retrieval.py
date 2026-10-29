"""Small offline retrieval for RTL design guidance.

The competition image is offline, so retrieval is deterministic and needs no
embedding model, vector service, or network access. Only a few short entries are
inserted into each model request; full references stay on disk.
"""

from __future__ import annotations

from pathlib import Path
import re


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+")
_STOPWORDS = {"a", "an", "the", "and", "or", "with", "for", "to", "of",
              "module", "design", "implement", "implementation", "rtl",
              "verilog", "systemverilog", "please", "write", "make",
              "input", "output", "inout", "port", "ports", "interface",
              "bits", "bit", "topmodule", "named", "following", "all",
              "one", "two", "given", "should", "must", "is", "be", "it",
              "this", "that", "when", "then", "on", "at", "as", "by"}


def routing_text(text: str) -> str:
    """Do not route explicit absence of clocks/resets as a request for them."""
    return re.sub(
        r"\b(?:no|without)\s+(?:(?:a|any)\s+)?(?:clock|reset)"
        r"(?:\s*(?:,|or|and|/)\s*(?:a\s+)?(?:clock|reset))*\b",
        " ", text, flags=re.IGNORECASE,
    )


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    # Normalize identifiers and numeric suffixes, e.g. shift_register/lfsr16.
    values = _TOKEN.findall(text.lower())
    values += [part for value in values for part in value.split("_")]
    values += [re.sub(r"\d+$", "", value) for value in values]
    for value in values:
        if not value or value in _STOPWORDS:
            continue
        terms.add(value)
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            terms.update(value[i:i + 2] for i in range(len(value) - 1))
    return terms


def retrieve(query: str, root: str | Path, *, limit: int = 2,
             max_chars: int = 1800, task: str = "") -> list[dict]:
    """Return the most relevant short Markdown notes, with stable tie breaks."""
    query_terms = _terms(routing_text(query))
    task_terms = _terms(routing_text(task))
    if not query_terms:
        return []

    results = []
    base = Path(root)
    if not base.is_dir():
        return []
    for path in sorted(base.rglob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        title = next((line.lstrip("# ").strip() for line in content.splitlines()
                      if line.startswith("#")), path.stem)
        keywords_line = next((line.split(":", 1)[1] for line in content.splitlines()
                              if line.lower().startswith("keywords:")), "")
        # Curated metadata gates relevance. Common body words must not make
        # unrelated notes appear in every request. No metadata means no RAG.
        matches = query_terms & _terms(keywords_line)
        if not matches:
            continue
        title_hits = len(matches & _terms(title))
        task_matches = task_terms & _terms(keywords_line)
        # Keep the task's operation ahead of incidental terms in a tool log.
        score = title_hits * 4 + len(matches) * 3 + len(task_matches) * 12
        if score <= 0:
            continue
        body = "\n".join(line for line in content.splitlines()
                         if not line.startswith("# ")
                         and not line.lower().startswith("keywords:")).strip()
        excerpt = body[:max_chars]
        if len(body) > max_chars:
            excerpt += "\n...[entry truncated]"
        results.append({"id": path.relative_to(base).with_suffix("").as_posix(),
                        "title": title, "score": score, "matches": sorted(matches),
                        "task_matches": sorted(task_matches),
                        "content": excerpt})
    results.sort(key=lambda row: (-row["score"], row["id"]))
    return results[:max(0, limit)]
