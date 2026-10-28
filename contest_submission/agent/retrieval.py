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
              "bits", "bit"}


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for value in _TOKEN.findall(text.lower()):
        if value in _STOPWORDS:
            continue
        terms.add(value)
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            terms.update(value[i:i + 2] for i in range(len(value) - 1))
    return terms


def retrieve(query: str, root: str | Path, *, limit: int = 2,
             max_chars: int = 1800) -> list[dict]:
    """Return the most relevant short Markdown notes, with stable tie breaks."""
    query_terms = _terms(query)
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
        body_terms = _terms(content)
        title_hits = len(query_terms & _terms(title))
        keyword_hits = len(query_terms & _terms(keywords_line))
        body_hits = len(query_terms & body_terms)
        score = title_hits * 4 + keyword_hits * 3 + body_hits
        if score <= 0:
            continue
        excerpt = content[:max_chars]
        if len(content) > max_chars:
            excerpt += "\n...[entry truncated]"
        results.append({"id": path.relative_to(base).with_suffix("").as_posix(),
                        "title": title, "score": score, "content": excerpt})
    results.sort(key=lambda row: (-row["score"], row["id"]))
    return results[:max(0, limit)]
