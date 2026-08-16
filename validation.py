"""Server-side enforcement of the OKF / AI-First vault invariants.

OKF is the Open Knowledge Format: a Markdown file that reads well for a human and
parses reliably for a machine. Here that means frontmatter carrying date/type/tags/
ai-first, an H1 title, and an `## AI Summary` preamble (see ADR 5).

This module enforces the OKF/AI-first invariants as mechanical, testable checks,
independent of any client prompt. Pure functions, no I/O (the known-title set is
passed in), so each rule has its own unit test.

Design boundary ("Validating Server, Smart Client"): every rule here checks whether
content is WELL-FORMED, never whether it is TRUE or worth saving. Heuristic rules
default to "warn"; only the unambiguous schema rule defaults to "error".
"""
import re
from dataclasses import dataclass, field

import config


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed_rules: set = field(default_factory=set)  # rule names that produced an error

    @property
    def ok(self) -> bool:
        return not self.errors

    def merge(self, other: "Result") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.failed_rules |= other.failed_rules

    def emit(self, strength: str, message: str, rule: str = "") -> None:
        """Record a message at the configured strength (error/warn/off)."""
        if strength == "error":
            self.errors.append(message)
            if rule:
                self.failed_rules.add(rule)
        elif strength == "warn":
            self.warnings.append(message)
        # "off" → ignore


_URL_RE = re.compile(r"https?://\S+")
# A recency marker near a source: a full date or an "(as of YYYY-MM…)" tag.
_RECENCY_RE = re.compile(r"\b\d{4}-\d{2}(-\d{2})?\b|\(as of \d{4}", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"\(confidence:\s*(stated|high|medium|speculation)\)", re.IGNORECASE)
_SPECULATION_RE = re.compile(
    r"\b(maybe|perhaps|probably|i think|i believe|likely|presumably|might be|could be|seems? to)\b",
    re.IGNORECASE,
)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)


# ---- individual rules ------------------------------------------------------

def _rule_schema(vault: str, type_meta: str, tags, title: str) -> Result:
    """Schema completeness — the unambiguous, always-checkable invariant."""
    r = Result()
    s = config.rule_strength("schema")
    if vault not in ("brain", "context"):
        r.emit(s, f"Invalid vault '{vault}'. Must be 'brain' or 'context'.", "schema")
    if type_meta not in config.NOTE_TYPES:
        r.emit(s, f"Invalid type_meta '{type_meta}'. Allowed: {', '.join(config.NOTE_TYPES)}.", "schema")
    if not isinstance(tags, list):
        r.emit(s, "tags must be a list of strings.", "schema")
    if not title or not title.strip():
        r.emit(s, "title must not be empty.", "schema")
    return r


def _rule_preamble(content: str) -> Result:
    """Every note must carry the '## AI Summary' structural block."""
    r = Result()
    heading = config.OKF_PREAMBLE_HEADING.lower()
    if heading not in content.lower():
        r.emit(config.rule_strength("preamble"),
               f"Missing required section '{config.OKF_PREAMBLE_HEADING}'. "
               f"Add a short block summarizing context for a future AI session, e.g.\n"
               f"{config.OKF_PREAMBLE_HEADING}\n<one or two sentences of context>", "preamble")
    return r


def _rule_sources(content: str) -> Result:
    """External facts (raw URLs) should carry a recency/date marker nearby."""
    r = Result()
    if _URL_RE.search(content) and not _RECENCY_RE.search(content):
        r.emit(config.rule_strength("sources"),
               "Contains external URLs but no recency marker. Add a date next to "
               "sources, e.g. '(as of 2026-06, https://…)'.", "sources")
    return r


def _rule_confidence(content: str) -> Result:
    """Speculative/inferred claims (or external sources) want a confidence tag."""
    r = Result()
    needs = bool(_SPECULATION_RE.search(content) or _URL_RE.search(content))
    if needs and not _CONFIDENCE_RE.search(content):
        r.emit(config.rule_strength("confidence"),
               "Speculative or sourced claims found without a confidence marker. "
               "Add '(confidence: stated|high|medium|speculation)'.", "confidence")
    return r


# ---- Mechanical autolink against KNOWN note titles --------------------

def autolink(content: str, known_titles) -> tuple[str, list[str]]:
    """Link mentions of EXISTING notes. Returns (new_content, linked_titles).

    Deterministic and idempotent: only exact, word-boundary, case-insensitive
    title matches that are not already inside a [[…]] are linked. Never invents
    links to non-existent notes. Longest titles first so 'Project Phoenix' wins
    over 'Phoenix'.
    """
    linked: list[str] = []
    if not known_titles:
        return content, linked

    # Spans already covered by existing wikilinks — never touch those.
    protected = [(m.start(), m.end()) for m in _WIKILINK_RE.finditer(content)]

    def _in_protected(pos: int) -> bool:
        return any(a <= pos < b for a, b in protected)

    for title in sorted(known_titles, key=len, reverse=True):
        title = title.strip()
        if not title:
            continue
        pattern = re.compile(rf"(?<!\[)\b{re.escape(title)}\b(?!\])", re.IGNORECASE)
        # Find a linkable occurrence (not inside an existing link).
        for m in pattern.finditer(content):
            if _in_protected(m.start()):
                continue
            content = content[:m.start()] + f"[[{title}]]" + content[m.end():]
            linked.append(title)
            # Recompute protected spans after mutation; one link per title is enough.
            protected = [(mm.start(), mm.end()) for mm in _WIKILINK_RE.finditer(content)]
            break
    return content, linked


def suggest_links(content: str, known_titles) -> list[str]:
    """(Warn mode): which existing notes are mentioned but not yet linked."""
    suggestions: list[str] = []
    for title in sorted(known_titles, key=len, reverse=True):
        title = title.strip()
        if not title:
            continue
        mentioned = re.search(rf"(?<!\[)\b{re.escape(title)}\b(?!\])", content, re.IGNORECASE)
        already = re.search(rf"\[\[{re.escape(title)}\]\]", content, re.IGNORECASE)
        if mentioned and not already:
            suggestions.append(title)
    return suggestions


# ---- orchestrator ----------------------------------------------------------

def validate_note(vault: str, type_meta: str, tags, content: str, title: str) -> Result:
    """Run every content/schema rule. Each rule is isolated so one buggy rule
    cannot crash the whole validation (fail-safe)."""
    result = Result()
    for rule, args in (
        (_rule_schema, (vault, type_meta, tags, title)),
        (_rule_preamble, (content,)),
        (_rule_sources, (content,)),
        (_rule_confidence, (content,)),
    ):
        try:
            result.merge(rule(*args))
        except Exception:  # A rule bug degrades to a warning, never a false block
            config.log.exception("Validation rule %s failed", getattr(rule, "__name__", rule))
            result.warnings.append("A validation rule could not be evaluated (see server log).")
    return result
