"""Offline knowledge base retrieval."""
from pathlib import Path
import re

from .storage import read_text


DEFAULT_KNOWLEDGE_ROOT = Path(__file__).resolve().parent.parent / "knowledge"
DEFAULT_SKILL_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _terms(text):
    terms = set()
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+", text):
        token = token.lower()
        terms.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            # Chinese has no whitespace between words. Add overlapping bigrams
            # so a short query can match a longer knowledge-base heading.
            terms.update(token[index:index + 2] for index in range(len(token) - 1))
    return terms


def _roots(root=None):
    if root is not None:
        return [Path(root)]
    return [DEFAULT_KNOWLEDGE_ROOT, DEFAULT_SKILL_ROOT]


def search(query, root=None, *, limit=3, max_chars=6000):
    terms = _terms(query)
    matches = []
    for base in _roots(root):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            text = read_text(path)
            title = next((line.lstrip("# ").strip() for line in text.splitlines()
                          if line.startswith("#")), path.stem)
            doc_terms = _terms(text)
            title_terms = _terms(title)
            score = len(terms & doc_terms) + 3 * len(terms & title_terms)
            if score or not terms:
                relative_path = path.relative_to(base)
                relative = (relative_path.parent.as_posix()
                            if relative_path.name.lower() == "skill.md"
                            else relative_path.with_suffix("").as_posix())
                if base == DEFAULT_SKILL_ROOT:
                    relative = "skill:" + relative
                matches.append((score, relative, title, text))
    matches.sort(key=lambda item: (-item[0], item[1]))
    results = []
    for score, identifier, title, text in matches[:limit]:
        excerpt = text[:max_chars]
        if len(text) > max_chars:
            excerpt += "\n...[entry truncated]"
        results.append({"id": identifier, "title": title, "score": score, "content": excerpt})
    return results


def index(root=None):
    result = []
    for base in _roots(root):
        if not base.is_dir():
            continue
        is_skill = base == DEFAULT_SKILL_ROOT
        for path in sorted(base.rglob("*.md")):
            relative_path = path.relative_to(base)
            identifier = (relative_path.parent.as_posix()
                          if relative_path.name.lower() == "skill.md"
                          else relative_path.with_suffix("").as_posix())
            if is_skill:
                identifier = "skill:" + identifier
            result.append({"id": identifier,
                           "path": path.relative_to(base).as_posix(),
                           "kind": "skill" if is_skill else "knowledge"})
    return result
