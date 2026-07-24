"""Deterministic natural-language front-end for canonical queries.

`graphite query --natural "<question>"` runs a fixed, grammar-based intent
parser: an ordered table of anchored regular expressions, first match wins.
Recognized question shapes translate into canonical query plans (executed
through the same validated path as structured queries) or into a suggestion
for the canonical command that answers them (impact/context/tests). Anything
else falls back to deterministic ranked search, whose results serve as
clarification candidates.

Canonical-safe by construction: no providers, no network, no configuration
knobs — the grammar below is the entire capability, and it is published via
`graphite capabilities` so agents never have to guess phrasings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import networkx as nx

from .query import _VERB_INDEX, execute_plan, search_graph
from .query_plan import make_plan

RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class NaturalRule:
    """One grammar rule: intent plus the anchored pattern that recognizes it."""

    intent: str  # query operation name, or "impact" | "tests" | "context"
    kind: str  # "operation" (translates to a plan) | "command" (suggests a CLI command)
    template: str  # human-readable form published via capabilities
    pattern: re.Pattern[str]


def _rule(intent: str, kind: str, template: str, pattern: str) -> NaturalRule:
    return NaturalRule(intent, kind, template, re.compile(pattern))


# Ordered, first match wins; every pattern is fullmatched against the
# normalized (lowercased, squashed, unpunctuated) question.
NATURAL_RULES: tuple[NaturalRule, ...] = (
    _rule("callers", "operation", "who calls <symbol>", r"(?:who|what) calls (?P<node>.+)"),
    _rule("callers", "operation", "callers of <symbol>", r"(?:list |show )?callers of (?P<node>.+)"),
    _rule("callers", "operation", "who uses <symbol>", r"(?:who|what) uses (?P<node>.+)"),
    _rule("calls", "operation", "what does <symbol> call", r"what does (?P<node>.+?) call"),
    _rule("calls", "operation", "callees of <symbol>", r"(?:list |show )?callees of (?P<node>.+)"),
    _rule(
        "depends-on", "operation", "what does <node> depend on",
        r"what does (?P<node>.+?) depend on",
    ),
    _rule(
        "depends-on", "operation", "dependencies of <node>",
        r"(?:list |show )?dependencies of (?P<node>.+)",
    ),
    _rule(
        "depends-on", "operation", "what does <node> import",
        r"what does (?P<node>.+?) import",
    ),
    _rule("imported-by", "operation", "what depends on <node>", r"what depends on (?P<node>.+)"),
    _rule("imported-by", "operation", "who imports <node>", r"(?:who|what) imports (?P<node>.+)"),
    _rule(
        "imported-by", "operation", "dependents of <node>",
        r"(?:list |show )?dependents of (?P<node>.+)",
    ),
    _rule(
        "imported-by", "operation", "where is <node> imported",
        r"where is (?P<node>.+?) (?:imported|used)",
    ),
    _rule(
        "reaches", "operation", "can <a> reach <b>",
        r"can (?P<source>.+?) reach (?P<target>.+)",
    ),
    _rule(
        "reaches", "operation", "is there a call path from <a> to <b>",
        r"is there a call path from (?P<source>.+?) to (?P<target>.+)",
    ),
    _rule(
        "reaches", "operation", "does <a> call <b>",
        r"does (?P<source>.+?) (?:eventually |transitively )?call (?P<target>.+)",
    ),
    _rule(
        "path", "operation", "path from <a> to <b>",
        r"(?:shortest |show |find )*path from (?P<source>.+?) to (?P<target>.+)",
    ),
    _rule(
        "path", "operation", "is there a path from <a> to <b>",
        r"is there a path from (?P<source>.+?) to (?P<target>.+)",
    ),
    _rule(
        "path", "operation", "how does <a> reach <b>",
        r"how does (?P<source>.+?) reach (?P<target>.+)",
    ),
    _rule(
        "stats", "operation", "graph stats",
        r"(?:graph )?(?:stats|statistics|overview|summary)",
    ),
    _rule("stats", "operation", "how big is the graph", r"how big is the graph"),
    _rule(
        "impact", "command", "what breaks if i change <path>",
        r"what (?:breaks|is affected|gets affected) (?:if|when) (?:i )?(?:change|modify|edit|touch) (?P<node>.+)",
    ),
    _rule(
        "impact", "command", "impact of changing <path>",
        r"impact of (?:changing |modifying |editing )?(?P<node>.+)",
    ),
    _rule("impact", "command", "blast radius of <path>", r"blast radius (?:of|for) (?P<node>.+)"),
    _rule(
        "tests", "command", "what tests cover <path>",
        r"(?:what|which) tests (?:cover|exercise|test) (?P<node>.+)",
    ),
    _rule(
        "tests", "command", "tests for <path>",
        r"(?:what |which )?tests (?:for|to run for) (?P<node>.+)",
    ),
    _rule(
        "context", "command", "context for <node>",
        r"(?:show |give |get )?(?:me )?context (?:for|of|about) (?P<node>.+)",
    ),
    _rule("context", "command", "tell me about <node>", r"tell me about (?P<node>.+)"),
)

_COMMAND_SUGGESTIONS: dict[str, tuple[str, str]] = {
    "impact": ("impact", "impact analysis runs as its own canonical command"),
    "tests": ("impact", "run graphite impact; its likely_tests field lists tests to run"),
    "context": ("context", "context runs as its own canonical command"),
}

_ARTICLES = ("the ", "a ", "an ", "this ", "my ", "our ")
_DESCRIPTORS = (" function", " method", " class", " file", " module", " node", " symbol")

_STOPWORDS = frozenset(
    "a an and are can could did do does for from give how i in is it list me my of on or our "
    "please should show tell that the this to was we what when where which who whom whose why "
    "will with would you your about find".split()
)


def _normalize(question: str) -> str:
    text = re.sub(r"\s+", " ", question.strip().lower()).strip("\"'")
    return re.sub(r"[?!.]+$", "", text).strip()


def _clean_target(text: str) -> str:
    cleaned = text.strip().strip("`\"'")
    if cleaned in {article.strip() for article in _ARTICLES}:
        return ""
    for article in _ARTICLES:
        if cleaned.startswith(article):
            cleaned = cleaned[len(article):]
            break
    for descriptor in _DESCRIPTORS:
        if cleaned.endswith(descriptor):
            cleaned = cleaned[: -len(descriptor)]
            break
    return cleaned.strip().strip("`\"'")


def natural_catalog() -> list[dict[str, Any]]:
    """Machine-readable grammar listing, grouped by intent in rule order."""
    catalog: list[dict[str, Any]] = []
    for rule in NATURAL_RULES:
        entry = next((e for e in catalog if e["intent"] == rule.intent), None)
        if entry is None:
            entry = {"intent": rule.intent, "kind": rule.kind, "templates": []}
            catalog.append(entry)
        entry["templates"].append(rule.template)
    return catalog


def _search_fallback(question: str, normalized: str) -> dict[str, Any]:
    terms = " ".join(w for w in normalized.split() if w not in _STOPWORDS)
    if not terms:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "error": "could not extract search terms from question",
            "error_code": "natural_no_terms",
            "suggestions": [
                "Ask about a concrete symbol, file, or path",
                "Run `graphite capabilities --json` to list the supported question grammar",
            ],
        }
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "natural": {"question": question, "intent": "search", "pattern": None},
        "suggestion": {
            "command": ["graphite", "search", terms],
            "reason": "no grammar pattern matched; ranked search results serve as clarification candidates",
        },
    }


def translate_natural(question: str) -> dict[str, Any]:
    """Deterministically translate a question, without touching any graph.

    Returns one of: {natural, plan} for verb intents; {natural, suggestion} for
    command intents and the search fallback; or an error dict with a stable
    error_code. Never raises, never does inference.
    """
    normalized = _normalize(question)
    if not normalized:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "error": "empty query",
            "error_code": "empty_query",
        }
    for rule in NATURAL_RULES:
        m = rule.pattern.fullmatch(normalized)
        if m is None:
            continue
        captured = {key: _clean_target(value) for key, value in m.groupdict().items()}
        if any(not value for value in captured.values()):
            break  # degenerate capture (e.g. "who calls the"); fail closed to search
        natural = {"question": question, "intent": rule.intent, "pattern": rule.template}
        if rule.kind == "command":
            command, reason = _COMMAND_SUGGESTIONS[rule.intent]
            return {
                "schema_version": RESULT_SCHEMA_VERSION,
                "natural": natural,
                "suggestion": {
                    "command": ["graphite", command, captured["node"]],
                    "reason": reason,
                },
            }
        spec = _VERB_INDEX[rule.intent]
        extracted = [captured[key] for key in ("node", "source", "target") if key in captured]
        targets = list(zip(spec.roles, extracted))
        plan = make_plan(spec.name, targets, dict(spec.limits))
        return {"schema_version": RESULT_SCHEMA_VERSION, "natural": natural, "plan": plan}
    return _search_fallback(question, normalized)


def answer_natural(g: nx.DiGraph, question: str) -> dict[str, Any]:
    """Translate and answer: execute the plan, or run the search fallback.

    Verb-intent errors (node_not_found with candidates, no_path, ...) pass
    through the normal query envelope with the natural interpretation and plan
    attached, so a failed resolution still explains itself.
    """
    translated = translate_natural(question)
    if "error" in translated:
        return translated
    if "plan" in translated:
        result = execute_plan(g, translated["plan"])
        return {**result, "plan": translated["plan"], "natural": translated["natural"]}
    if translated["natural"]["intent"] == "search":
        terms = translated["suggestion"]["command"][2]
        return {**translated, "search": search_graph(g, terms)}
    return translated
