from __future__ import annotations

import re
from typing import Any

from ..config import normalize_text
from ..domain import PolicyDecision


def match_rules(text: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    value = normalize_text(text).casefold()
    matches = []
    for rule in rules:
        if not rule.get("enabled", True): continue
        keyword = normalize_text(str(rule.get("keyword", ""))).casefold()
        if not keyword: continue
        if rule.get("match", "word_boundary") == "word_boundary":
            matched = bool(re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", value))
        else:
            matched = keyword in value
        if matched:
            matches.append({"id": rule.get("id", ""), "keyword": keyword, "score": int(rule.get("score", 0)), "reason": rule.get("reason", "")})
    return sorted(matches, key=lambda x: (-x["score"], x["id"]))


def _priority(score: int, thresholds: dict[str, int]) -> str:
    if score >= thresholds.get("high", 80): return "high"
    if score >= thresholds.get("medium", 40): return "medium"
    return "low"


def evaluate(text: str, model_priority: str | None, policy: dict[str, Any]) -> PolicyDecision:
    thresholds = policy.get("thresholds", {"high": 80, "medium": 40})
    matched = match_rules(text, policy.get("rules", []))
    rule_priority = _priority(matched[0]["score"], thresholds) if matched else "low"
    if policy.get("strategy", "model_first") == "rules_override" and matched:
        proposed, source = rule_priority, "policy"
    elif model_priority in {"low", "medium", "high"}:
        proposed, source = model_priority, "model"
    elif matched:
        proposed, source = rule_priority, "policy"
    else:
        proposed, source = "low", "builtin"
    removal = next((r for r in policy.get("removals", []) if r.get("enabled", True) and normalize_text(r.get("text", "")).casefold() in normalize_text(text).casefold()), None)
    return PolicyDecision(not bool(removal), removal.get("reason") if removal else None, rule_priority, proposed, source, tuple(matched))


def validate_policy(strategy: str, rules: list[dict[str, Any]], removals: list[dict[str, Any]]) -> None:
    if strategy not in {"model_first", "rules_override"}: raise ValueError("invalid policy strategy")
    if len(rules) > 1000 or len(removals) > 1000: raise ValueError("policy has too many entries")
    ids = [r.get("id") for r in rules]
    if any(not x for x in ids) or len(set(ids)) != len(ids): raise ValueError("rule ids must be unique")
    for rule in rules:
        if not rule.get("keyword") or not 0 <= int(rule.get("score", -1)) <= 100: raise ValueError("invalid policy rule")
    rids = [r.get("id") for r in removals]
    if any(not x for x in rids) or len(set(rids)) != len(rids): raise ValueError("removal ids must be unique")
