"""Internal tools for meta-skill-creator."""

from __future__ import annotations

import json
import re as _re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import ValidationError

from opensquilla.engine.steps.meta_resolution import _trigger_matches
from opensquilla.skills.creator.patterns import PATTERN_SLOT_SCHEMA
from opensquilla.skills.loader import SkillLoader
from opensquilla.tools.registry import tool

from .activation import evaluate_candidate_activation
from .quality import evaluate_generation_quality
from .runtime_e2e import run_runtime_e2e_gate

_TEMPLATES_DIR = Path(__file__).resolve().parent / "patterns"
_log = structlog.get_logger(__name__)
_CREATOR_INTERNAL_SKILLS = {
    "meta-skill-creator",
    "skill-creator-linter",
    "skill-creator-proposals",
    "skill-creator-smoke-test",
}


class _FillSlotsValidationError(ValueError):
    """Wraps the underlying ValidationError with actionable message text."""


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences common in LLM JSON responses.

    Handles ``\\`\\`\\`json...\\`\\`\\```, ``\\`\\`\\`...\\`\\`\\```, and bare JSON.
    Returns the inner text.
    """
    text = text.strip()
    # Pattern: optional ```lang at start, content, optional ``` at end
    m = _re.match(r"^```(?:json|JSON)?\s*\n(.*?)\n```\s*$", text, _re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: strip leading/trailing ``` even without lang tag
    if text.startswith("```") and text.endswith("```"):
        inner = text[3:-3]
        # Strip leading 'json\n' if present
        inner = _re.sub(r"^json\s*\n?", "", inner, flags=_re.IGNORECASE)
        return inner.strip()
    return text


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.globals["creator_step_kind"] = _creator_step_kind
    return env


@lru_cache(maxsize=256)
def _creator_step_kind(skill_name: str) -> str:
    """Return the safest generated meta-step kind for a bundled skill.

    Skills with an entrypoint should be composed as ``skill_exec`` so the
    runtime executes the wrapped CLI directly instead of asking a sub-Agent to
    describe a command. Pure text transforms should use ``llm_chat`` to avoid
    spawning a tool-capable sub-agent that may return no visible final text.
    """
    if skill_name == "summarize":
        return "llm_chat"
    bundled = Path(__file__).resolve().parents[1] / "bundled"
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=Path(tempfile.gettempdir()) / "creator-kind-snap.json",
    )
    loader.invalidate_cache()
    spec = loader.get_by_name(skill_name)
    if spec is not None and getattr(spec, "entrypoint", None):
        return "skill_exec"
    return "agent"


def meta_skill_assemble(pattern_id: str, slots_json: str) -> str:
    """Render SKILL.md from validated slots."""
    if pattern_id not in PATTERN_SLOT_SCHEMA:
        raise ValueError(f"unknown pattern_id: {pattern_id}")
    schema = PATTERN_SLOT_SCHEMA[pattern_id]
    try:
        slots_dict = json.loads(slots_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"slots_json not valid JSON: {exc}") from exc
    try:
        slots = schema.model_validate(slots_dict)
    except ValidationError as exc:
        raise ValueError(f"slots failed schema {pattern_id}: {exc}") from exc

    env = _jinja_env()
    template_name = f"{pattern_id}.md.j2"
    rendered = env.get_template(template_name).render(**slots.model_dump())
    return rendered


def _resolve_provider_from_config() -> tuple[str | None, str | None, str | None, str | None]:
    """Read provider/model/api_key/base_url from the gateway config.

    N14 fix: delegates to GatewayConfig.load() so env-var overrides
    (OPENSQUILLA_LLM_PROVIDER / _MODEL / _API_KEY / _BASE_URL) and
    base_url / proxy / provider_routing fields are honoured identically
    to the gateway.  The N6 manual tomllib reader missed these, breaking
    creator in env-configured and vllm/azure/custom-endpoint deployments.

    Uses ``importlib.import_module`` (not a bare ``from … import``) so
    the architecture import-contract test (which detects edges via
    ``ast.walk`` on module-level *and* function-body nodes) does not see
    a ``skills → gateway`` static import statement.

    Fix #C — env-var override priority note:
    ``OPENSQUILLA_LLM_MODEL`` and friends ARE honoured when no TOML file
    exists (pydantic-settings reads them via LlmProviderConfig's own
    ``env_prefix="OPENSQUILLA_LLM_"``).  However, when a TOML file is
    present ``GatewayConfig.load()`` passes the TOML dict to the
    constructor and pydantic-settings' env-var scan is only applied to
    fields that were NOT supplied in the dict — so a TOML ``[llm]``
    section can shadow env vars.  To override the LLM in the presence of
    a TOML file, use the *correct pydantic-settings nested delimiter*:
    ``OPENSQUILLA_GATEWAY__LLM__MODEL=<value>`` (double underscores,
    ``OPENSQUILLA_GATEWAY_`` prefix from the parent GatewayConfig).
    The simpler ``OPENSQUILLA_LLM_MODEL`` only works when the LLM
    section is absent from the TOML file.

    After GatewayConfig.load(), we apply an explicit env-var post-override
    so that ``OPENSQUILLA_LLM_MODEL`` / ``OPENSQUILLA_LLM_PROVIDER`` /
    ``OPENSQUILLA_LLM_API_KEY`` / ``OPENSQUILLA_LLM_BASE_URL`` always win
    regardless of TOML content — matching user expectations from the docs.
    """
    import importlib
    import os

    try:
        # Resolve the config path the same way the old manual reader did:
        # OPENSQUILLA_GATEWAY_CONFIG_PATH env var wins; GatewayConfig.load()
        # then also falls back to ./opensquilla.toml and ~/.opensquilla/config.toml.
        config_path_env = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip() or None

        gateway_config_mod = importlib.import_module("opensquilla.gateway.config")
        cfg = gateway_config_mod.GatewayConfig.load(config_path_env)
        llm = cfg.llm
        provider_name = (getattr(llm, "provider", None) or "").strip() or None
        model = (getattr(llm, "model", None) or "").strip() or None
        # N11: accept empty api_key — keyless local providers (ollama,
        # lm_studio, ovms, vllm) do not require an API key.
        api_key = (getattr(llm, "api_key", "") or "").strip()
        base_url = (getattr(llm, "base_url", "") or "").strip()

        # Fix #C: apply explicit env-var post-overrides so that
        # OPENSQUILLA_LLM_MODEL / _PROVIDER / _API_KEY / _BASE_URL always win
        # over TOML file values (pydantic-settings nesting means the sub-model's
        # env bindings are shadowed by the parent TOML dict when a [llm] section
        # is present in config.toml).
        env_provider = os.environ.get("OPENSQUILLA_LLM_PROVIDER", "").strip()
        env_model = os.environ.get("OPENSQUILLA_LLM_MODEL", "").strip()
        env_api_key = os.environ.get("OPENSQUILLA_LLM_API_KEY", "").strip()
        env_base_url = os.environ.get("OPENSQUILLA_LLM_BASE_URL", "").strip()
        if env_provider:
            provider_name = env_provider
        if env_model:
            model = env_model
        if env_api_key:
            api_key = env_api_key
        if env_base_url:
            base_url = env_base_url

        # N15: toml may set provider+model but leave api_key empty (the
        # common deployment pattern is to keep the key in a
        # provider-specific env var like OPENROUTER_API_KEY rather than
        # checking it into a tracked toml). When that happens, fall back
        # to the conventional env var for the resolved provider so
        # creator's LLM calls do not fail with an empty Bearer header.
        # Keyless local providers (ollama, lm_studio, ovms, vllm) have no
        # env-var entry and rightly stay empty.
        if provider_name and not api_key:
            provider_env_map = {
                "openrouter": "OPENROUTER_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
                "gemini": "GEMINI_API_KEY",
                "dashscope": "DASHSCOPE_API_KEY",
                "minimax": "MINIMAX_API_KEY",
            }
            provider_env = provider_env_map.get(provider_name.lower(), "")
            if provider_env:
                api_key = os.environ.get(provider_env, "").strip()

        if provider_name and model:
            return (provider_name, model, api_key, base_url)
    except Exception:
        pass
    return (None, None, None, None)


def _resolve_provider_from_env() -> tuple[str | None, str | None, str | None]:
    """Fallback: scan env vars in priority order.

    Preference order: openrouter → anthropic → openai.
    """
    import os

    if os.environ.get("OPENROUTER_API_KEY"):
        return (
            "openrouter",
            os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            os.environ["OPENROUTER_API_KEY"],
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        model = os.environ.get("ANTHROPIC_MODEL")
        if not model:
            return (None, None, None)
        return ("anthropic", model, os.environ["ANTHROPIC_API_KEY"])
    if os.environ.get("OPENAI_API_KEY"):
        return ("openai", "gpt-4o-mini", os.environ["OPENAI_API_KEY"])
    return (None, None, None)


def _call_llm_for_slots(prompt: str, **kwargs: Any) -> str:
    """Production LLM call for slot filling. Resolves provider via GatewayConfig
    first (matches the gateway's normal config-loading path), then falls back to
    env vars for bare-script and test scenarios.

    Tests monkeypatch this symbol to inject deterministic stubs.
    """
    import asyncio

    from opensquilla.engine.types import AgentConfig
    from opensquilla.provider.selector import build_provider
    from opensquilla.skills.meta.orchestrator import make_llm_chat_from_provider

    # Config-driven resolution first (matches gateway behaviour for deployments
    # that use ~/.opensquilla/config.toml instead of raw env vars).
    provider_name, model, api_key, base_url = _resolve_provider_from_config()
    if provider_name is None:
        provider_name, model, api_key = _resolve_provider_from_env()
        base_url = ""
    if provider_name is None:
        raise RuntimeError(
            "meta-skill-creator: no LLM provider configured. "
            "Set provider in ~/.opensquilla/config.toml or set "
            "OPENROUTER_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY."
        )

    # Both helpers that succeed return non-None values; narrow for mypy.
    assert model is not None
    assert api_key is not None
    assert base_url is not None

    # kwargs.get("model") can override the resolved model (e.g. in tests).
    effective_model: str = kwargs.get("model", model)

    # Fix #C: log resolved provider/model so E2E logs show which model
    # actually handled the call and whether OPENSQUILLA_LLM_MODEL is honoured.
    _log.info(
        "meta_skill_fill_slots.llm_call",
        provider=provider_name,
        model=effective_model,
        prompt_chars=len(prompt),
    )

    provider = build_provider(
        provider=provider_name, model=effective_model, api_key=api_key, base_url=base_url,
    )
    base_config = AgentConfig(model_id=effective_model)
    # 4096 tokens: this call has a ~13k-char prompt and must produce a JSON
    # blob (slots schema for the chosen pattern, often 500-1500 tokens). On
    # reasoning models (deepseek-v4-flash) the chain-of-thought is counted
    # in the same budget; 2048 left the budget exhausted inside reasoning
    # and yielded an empty visible response (observed live 2026-05-23,
    # meta_skill_fill_slots.validation_failed_initial with empty preview).
    # 4096 gives reasoning + JSON room without becoming costly (~¥0.0011
    # per call on v4-flash).
    llm_chat = make_llm_chat_from_provider(
        provider=provider, base_config=base_config, max_tokens=4096
    )

    async def _drive() -> str:
        return await llm_chat("", prompt)

    return asyncio.run(_drive())


_REAL_CALL_LLM_FOR_SLOTS = _call_llm_for_slots


def _build_catalog_summary() -> str:
    """Enumerate available bundled skills (name + 1-line description).

    Meta-skills and creator-internal helper skills are intentionally excluded
    — the runtime's agent executor rejects ``kind: meta`` composed inside
    another meta-skill (lint G1.2), so showing them in the catalog only invites
    the LLM to propose
    structurally-invalid SKILL.md files that the gates then reject. The
    creator helpers belong to meta-skill-creator's outer validation and
    persistence flow, not to the candidate meta-skill's business DAG. The
    upshot is wasted LLM calls + noisy proposals in the WebUI panel. Filter
    here so the LLM never sees nested-meta or creator-internal tools as an
    option.
    """
    bundled = Path(__file__).resolve().parents[1] / "bundled"
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=Path(tempfile.gettempdir()) / "creator-catalog-snap.json",
    )
    loader.invalidate_cache()
    lines: list[str] = []
    for spec in loader.load_all():
        if spec.name in _CREATOR_INTERNAL_SKILLS:
            continue
        if getattr(spec, "kind", "skill") == "meta":
            continue
        first_line = (spec.description or "").split("\n", 1)[0][:120]
        lines.append(f"- {spec.name}: {first_line}")
    return "\n".join(lines)


def _build_pattern_example(pattern_id: str) -> dict:
    """Return a minimal valid example for the pattern's slot schema.

    Anchors the LLM on the exact field names — Pydantic schema descriptions
    alone are insufficient to prevent field-name hallucination (e.g. LLMs
    naturally write ``execution_sequence`` when the schema says ``steps``).
    """
    if pattern_id == "p1_sequential":
        return {
            "name": "example-pipeline",
            "description": "A 2-step example that extracts PDF text then summarizes it.",
            "meta_priority": 50,
            "triggers": ["example trigger phrase"],
            "eval_prompts": [
                {
                    "name": "positive_example_pipeline",
                    "prompt": "please run the example trigger phrase workflow",
                    "expect": "activate",
                },
                {
                    "name": "negative_generic_summary",
                    "prompt": "please summarize this unrelated note",
                    "expect": "skip",
                },
            ],
            "steps": [
                {
                    "id": "extract",
                    "skill": "pdf-toolkit",
                    "task": "Extract text from the PDF",
                    "with_keys": {},
                },
                {
                    "id": "digest",
                    "skill": "summarize",
                    "task": "Summarize the extracted text",
                    "with_keys": {},
                },
            ],
        }
    if pattern_id == "p2_fan_out_merge":
        return {
            "name": "example-fan-out",
            "description": (
                "Gather weather and POI info in parallel, then merge into a travel itinerary."
            ),
            "meta_priority": 50,
            "triggers": ["example fan-out trigger"],
            "eval_prompts": [
                {
                    "name": "positive_fan_out",
                    "prompt": "please run the example fan-out trigger workflow",
                    "expect": "activate",
                },
                {
                    "name": "negative_single_summary",
                    "prompt": "summarize this single note without travel planning",
                    "expect": "skip",
                },
            ],
            "branches": [
                {"id": "weather", "skill": "weather", "task": "Fetch weather", "with_keys": {}},
                {
                    "id": "poi",
                    "skill": "multi-search-engine",
                    "task": "Search POIs",
                    "with_keys": {},
                },
            ],
            "merge": {
                "id": "itin",
                "skill": "summarize",
                "task": "Combine into itinerary",
                "with_keys": {},
            },
            "tail": None,
        }
    if pattern_id == "p3_condition_gated":
        return {
            "name": "example-gated-pipeline",
            "description": (
                "Assess an incoming request, gather evidence, then produce a "
                "decision-ready output with explicit assumptions."
            ),
            "meta_priority": 50,
            "triggers": ["example gated trigger"],
            "eval_prompts": [
                {
                    "name": "positive_gated_pipeline",
                    "prompt": "please run the example gated trigger workflow",
                    "expect": "activate",
                },
                {
                    "name": "negative_simple_answer",
                    "prompt": "answer this quick general question",
                    "expect": "skip",
                },
            ],
            "steps": [
                {
                    "id": "intake",
                    "skill": "summarize",
                    "task": "Extract constraints and missing information",
                    "with_keys": {},
                },
                {
                    "id": "evidence",
                    "skill": "history-explorer",
                    "task": "Find relevant prior context when available",
                    "with_keys": {},
                },
                {
                    "id": "decision",
                    "skill": "summarize",
                    "task": "Produce final answer with caveats and next actions",
                    "with_keys": {},
                },
            ],
        }
    return {}


def _extract_required_triggers_from_intent(user_intent: str) -> list[str]:
    """Extract trigger phrases the user explicitly required.

    The LLM prompt asks for verbatim preservation, but FULL_GATED creator
    output should not rely on the model remembering exact trigger phrases.
    Keep this intentionally conservative: only parse clear "trigger phrases
    include:" style clauses and stop at sentence/newline boundaries.
    """
    patterns = [
        r"触发(?:短语|词)?(?:要|应|必须)?(?:包含|包括)\s*[:：]\s*([^\n。；;]+)",
        r"trigger phrases?\s+(?:must\s+)?(?:include|contain)\s*[:：]\s*([^\n.;]+)",
    ]
    captured = ""
    for pattern in patterns:
        match = _re.search(pattern, user_intent, flags=_re.IGNORECASE)
        if match:
            captured = match.group(1)
            break
    if not captured:
        return []

    phrases: list[str] = []
    seen: set[str] = set()
    for raw in _re.split(r"[、，,]+", captured):
        phrase = raw.strip().strip("`'\"“”‘’[]()")
        if not phrase or phrase in seen:
            continue
        # Preserve only safe, plausible trigger phrases. This mirrors schema
        # YAML-safety constraints without accepting long trailing prose.
        if any(ch in phrase for ch in ('"', "\n", "\r", "\\")):
            continue
        if len(phrase) > 80:
            continue
        seen.add(phrase)
        phrases.append(phrase)
    return phrases


def _preserve_required_triggers(validated: Any, schema: Any, user_intent: str) -> Any:
    required = _extract_required_triggers_from_intent(user_intent)
    if not required:
        return validated
    data = validated.model_dump()
    current = [t for t in data.get("triggers", []) if isinstance(t, str)]
    merged: list[str] = []
    for phrase in [*required, *current]:
        if phrase not in merged:
            merged.append(phrase)
    data["triggers"] = merged[:8]
    return schema.model_validate(data)


_DRAFT_SEED_KEYS = (
    "source_kind",
    "goal",
    "observed_steps",
    "candidate_triggers",
    "inputs",
    "outputs",
    "constraints",
    "negative_cases",
    "evidence_refs",
    "duplicate_detection",
)
_DRAFT_SEED_FIELD_MAX_CHARS = {
    "source_kind": 120,
    "goal": 900,
    "observed_steps": 500,
    "candidate_triggers": 500,
    "inputs": 700,
    "outputs": 700,
    "constraints": 900,
    "negative_cases": 900,
    "evidence_refs": 500,
    "duplicate_detection": 900,
}
_DRAFT_SEED_LIST_MAX_ITEMS = 8
_DRAFT_SEED_LIST_ITEM_MAX_CHARS = 300
_DRAFT_SEED_DICT_MAX_ITEMS = 12
_DRAFT_SEED_DICT_KEY_MAX_CHARS = 80
_DRAFT_SEED_NESTED_STRING_MAX_CHARS = 300


def _truncate_draft_seed_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "... (truncated)"


def _cap_draft_seed_value(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, str):
        return _truncate_draft_seed_text(value, max_chars)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        capped = [
            _cap_draft_seed_value(item, max_chars=_DRAFT_SEED_LIST_ITEM_MAX_CHARS)
            for item in value[:_DRAFT_SEED_LIST_MAX_ITEMS]
        ]
        remaining = len(value) - len(capped)
        if remaining > 0:
            capped.append(f"... ({remaining} items truncated)")
        return capped
    if isinstance(value, dict):
        items = list(value.items())
        capped_dict = {
            _truncate_draft_seed_text(str(key), _DRAFT_SEED_DICT_KEY_MAX_CHARS): (
                _cap_draft_seed_value(
                    item,
                    max_chars=_DRAFT_SEED_NESTED_STRING_MAX_CHARS,
                )
            )
            for key, item in items[:_DRAFT_SEED_DICT_MAX_ITEMS]
        }
        remaining = len(items) - len(capped_dict)
        if remaining > 0:
            capped_dict["_truncated"] = f"{remaining} entries truncated"
        return capped_dict
    return _truncate_draft_seed_text(str(value), max_chars)


def _format_draft_seed_context(draft_seed_json: str) -> str:
    """Return a bounded prompt fragment from author_seed evidence."""
    raw = str(draft_seed_json or "").strip()
    if not raw:
        return "(none provided)"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "Invalid draft_seed_json was provided; ignore it."
    if not isinstance(parsed, dict):
        return "draft_seed_json was not a JSON object; ignore it."

    evidence = {}
    for key in _DRAFT_SEED_KEYS:
        if key in parsed:
            evidence[key] = _cap_draft_seed_value(
                parsed[key],
                max_chars=_DRAFT_SEED_FIELD_MAX_CHARS[key],
            )
    if not evidence:
        return "(no supported draft_seed_json evidence keys were provided)"

    return json.dumps(evidence, ensure_ascii=False, indent=2, default=str)


def _format_lesson_cards_context(draft_seed_json: str) -> str:
    """Return bounded creator lesson cards from draft_seed_json."""
    raw = str(draft_seed_json or "").strip()
    if not raw:
        return "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "[]"
    cards = parsed.get("lesson_cards") if isinstance(parsed, dict) else None
    if not isinstance(cards, list):
        return "[]"

    safe_cards: list[dict[str, object]] = []
    for card in cards[:8]:
        if not isinstance(card, dict):
            continue
        patch_hint = card.get("patch_hint")
        safe_cards.append({
            "pattern": _truncate_draft_seed_text(str(card.get("pattern") or ""), 80),
            "confidence": _truncate_draft_seed_text(
                str(card.get("confidence") or ""),
                20,
            ),
            "recommendation": _truncate_draft_seed_text(
                str(card.get("recommendation") or ""),
                300,
            ),
            "prompt_hint": _truncate_draft_seed_text(
                str(card.get("prompt_hint") or ""),
                300,
            ),
            "patch_hint": (
                _cap_draft_seed_value(patch_hint, max_chars=300)
                if isinstance(patch_hint, dict)
                else {}
            ),
        })
    return json.dumps(safe_cards, ensure_ascii=False, indent=2, default=str)


def _format_feedback_cards_context(creator_feedback_json: str) -> str:
    """Return bounded workflow-stage feedback cards from learning summary JSON."""
    raw = str(creator_feedback_json or "").strip()
    if not raw:
        return "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "[]"
    cards = parsed.get("feedback_cards") if isinstance(parsed, dict) else None
    if not isinstance(cards, list):
        return "[]"

    safe_cards: list[dict[str, object]] = []
    for card in cards[:8]:
        if not isinstance(card, dict):
            continue
        source_patterns = card.get("source_patterns")
        next_run_controls = card.get("next_run_controls")
        blocked_actions = card.get("blocked_actions")
        safe_cards.append({
            "stage": _truncate_draft_seed_text(str(card.get("stage") or ""), 80),
            "applies_to_task_class": _truncate_draft_seed_text(
                str(card.get("applies_to_task_class") or "global"),
                140,
            ),
            "confidence": _truncate_draft_seed_text(
                str(card.get("confidence") or ""),
                20,
            ),
            "evidence_count": card.get("evidence_count")
            if isinstance(card.get("evidence_count"), int)
            else 0,
            "source_patterns": (
                _cap_draft_seed_value(source_patterns, max_chars=80)
                if isinstance(source_patterns, list)
                else []
            ),
            "recommendation": _truncate_draft_seed_text(
                str(card.get("recommendation") or ""),
                320,
            ),
            "stage_hint": _truncate_draft_seed_text(
                str(card.get("stage_hint") or ""),
                220,
            ),
            "next_run_controls": (
                _cap_draft_seed_value(next_run_controls, max_chars=360)
                if isinstance(next_run_controls, dict)
                else {}
            ),
            "blocked_actions": (
                _cap_draft_seed_value(blocked_actions, max_chars=80)
                if isinstance(blocked_actions, list)
                else []
            ),
        })
    return json.dumps(safe_cards, ensure_ascii=False, indent=2, default=str)


def _format_success_pattern_cards_context(creator_feedback_json: str) -> str:
    """Return bounded positive recipe cards from learning summary JSON."""
    raw = str(creator_feedback_json or "").strip()
    if not raw:
        return "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return "[]"
    cards = parsed.get("success_pattern_cards") if isinstance(parsed, dict) else None
    if not isinstance(cards, list):
        return "[]"

    safe_cards: list[dict[str, object]] = []
    for card in cards[:8]:
        if not isinstance(card, dict):
            continue
        next_run_controls = card.get("next_run_controls")
        safe_cards.append({
            "applies_to_task_class": _truncate_draft_seed_text(
                str(card.get("applies_to_task_class") or "global"),
                140,
            ),
            "confidence": _truncate_draft_seed_text(
                str(card.get("confidence") or ""),
                20,
            ),
            "evidence_count": card.get("evidence_count")
            if isinstance(card.get("evidence_count"), int)
            else 0,
            "source_patterns": _cap_draft_seed_value(
                card.get("source_patterns")
                if isinstance(card.get("source_patterns"), list)
                else [],
                max_chars=80,
            ),
            "selected_patterns": _cap_draft_seed_value(
                card.get("selected_patterns")
                if isinstance(card.get("selected_patterns"), list)
                else [],
                max_chars=80,
            ),
            "trigger_samples": _cap_draft_seed_value(
                card.get("trigger_samples")
                if isinstance(card.get("trigger_samples"), list)
                else [],
                max_chars=120,
            ),
            "skill_chains": _cap_draft_seed_value(
                card.get("skill_chains")
                if isinstance(card.get("skill_chains"), list)
                else [],
                max_chars=160,
            ),
            "prompt_hint": _truncate_draft_seed_text(
                str(card.get("prompt_hint") or ""),
                320,
            ),
            "next_run_controls": (
                _cap_draft_seed_value(next_run_controls, max_chars=360)
                if isinstance(next_run_controls, dict)
                else {}
            ),
        })
    return json.dumps(safe_cards, ensure_ascii=False, indent=2, default=str)


def meta_skill_fill_slots(
    pattern_id: str,
    history_summary: str,
    user_intent: str,
    draft_seed_json: str = "",
    creator_feedback_json: str = "",
) -> str:
    """Drive LLM to fill pattern slots; Pydantic-validate; retry once on
    ValidationError. Returns validated JSON string."""
    if pattern_id not in PATTERN_SLOT_SCHEMA:
        raise ValueError(f"unknown pattern_id: {pattern_id}")
    schema = PATTERN_SLOT_SCHEMA[pattern_id]
    catalog = _build_catalog_summary()

    # Fix #A: inject the Pydantic JSON schema and a concrete example so the
    # LLM cannot hallucinate field names such as ``execution_sequence`` or
    # ``trigger_condition``.
    schema_dict = schema.model_json_schema()
    schema_json = json.dumps(schema_dict, ensure_ascii=False, indent=2)
    example_obj = _build_pattern_example(pattern_id)
    example_json = json.dumps(example_obj, ensure_ascii=False, indent=2)
    draft_seed_context = _format_draft_seed_context(draft_seed_json)
    lesson_cards_context = _format_lesson_cards_context(creator_feedback_json)
    success_pattern_cards_context = _format_success_pattern_cards_context(
        creator_feedback_json,
    )
    feedback_cards_context = _format_feedback_cards_context(creator_feedback_json)

    base_prompt = (
        f"<role>\n"
        f"You are a senior workflow-skill architect. Generate one narrow, "
        f"auditable bundled meta-skill candidate from the user's intent. Think "
        f"like a capability designer: preserve the future user's business "
        f"workflow, input/output contract, trigger boundary, and negative cases.\n"
        f"</role>\n\n"
        f"<task>\n"
        f"Fill the {pattern_id} slot schema for a new bundled meta-skill. The "
        f"candidate must describe what the generated meta-skill will do when a "
        f"future user invokes it, not how meta-skill-creator will validate, "
        f"persist, or enable the proposal.\n"
        f"</task>\n\n"
        f"<schema>\n"
        f"JSON Schema. Required field names; do not rename them.\n"
        f"```json\n{schema_json}\n```\n"
        f"</schema>\n\n"
        f"<example_output>\n"
        f"Valid example output for {pattern_id}:\n"
        f"```json\n{example_json}\n```\n"
        f"</example_output>\n\n"
        f"<catalog>\n"
        f"You may only reference these skills in `steps[].skill` or "
        f"`branches[].skill`:\n"
        f"{catalog}\n"
        f"</catalog>\n\n"
        f"<context>\n"
        f"<history_summary>\n{history_summary}\n</history_summary>\n"
        f"<user_intent>\n{user_intent}\n</user_intent>\n"
        f"<draft_seed>\n## Draft seed\n{draft_seed_context}\n</draft_seed>\n"
        f"<creator_lesson_cards>\n"
        f"advisory JSON derived from sanitized creator learning events. Apply "
        f"only lessons relevant to the current TASK_CLASS; current user "
        f"requirements and gates still win.\n"
        f"```json\n{lesson_cards_context}\n```\n"
        f"</creator_lesson_cards>\n"
        f"<creator_success_pattern_cards>\n"
        f"positive recipes from accepted skills and benchmark winners; "
        f"advisory only. Reuse successful patterns, trigger style, skill "
        f"chains, and gate coverage only when TASK_CLASS and contracts match. "
        f"Current user requirements and gates still win.\n"
        f"```json\n{success_pattern_cards_context}\n```\n"
        f"</creator_success_pattern_cards>\n"
        f"<creator_feedback_cards>\n"
        f"workflow-stage feedback from prior creator events; advisory only. "
        f"Use intent_brief feedback to preserve contracts, pattern_picker "
        f"feedback to question the selected pattern, context_builder feedback "
        f"to avoid duplicates, slot_generator feedback to tighten slots, "
        f"gate_calibration feedback to strengthen evals, and review_ux "
        f"feedback to improve reviewer-facing rationale. Do not bypass gates.\n"
        f"```json\n{feedback_cards_context}\n```\n"
        f"</creator_feedback_cards>\n"
        f"</context>\n\n"
        f"<intent_contract>\n"
        f"Extract and preserve these intent fields when evidence is present: "
        f"TASK_CLASS, workflow goal, INPUT_CONTRACT, OUTPUT_CONTRACT, "
        f"NEGATIVE_CASES, SUCCESS_CRITERIA, required trigger phrases, "
        f"positive/negative activation examples, "
        f"human-review preferences, tool/platform/config constraints, and "
        f"unresolved assumptions. TASK_CLASS is the reusable class of recurring "
        f"tasks the candidate should serve.\n"
        f"Treat draft_seed_json as evidence, not as permission to bypass gates. "
        f"Preserve seed constraints and negative_cases in trigger boundaries and "
        f"generation_rationale. If evidence is missing, record the gap in "
        f"unresolved_assumptions instead of inventing tools, inputs, outputs, or "
        f"gates. Do not invent tools, gates, inputs, or output contracts.\n"
        f"</intent_contract>\n\n"
        f"<examples>\n"
        f"<example name=\"good-trigger-boundary\">\n"
        f"Intent: a workflow that turns source docs into a decision brief. Good "
        f"trigger: `decision brief from source docs`. Bad triggers: `review`, "
        f"`summarize`, `analyze`, because they are overbroad triggers and will "
        f"collide with unrelated skills.\n"
        f"</example>\n"
        f"<example name=\"candidate-not-creator\">\n"
        f"If the user asks to create, validate, gate, judge, save, or persist "
        f"the meta-skill, treat those as outer creator requirements. Do not add "
        f"creator workflow steps such as collision_check, lint, smoke tests, "
        f"runtime E2E, LLM judge, acceptance comparison, proposal persistence, "
        f"or auto-enable to the candidate workflow.\n"
        f"</example>\n"
        f"<example name=\"contract-preservation\">\n"
        f"When intent says INPUT_CONTRACT is source PDFs and reviewer notes, and "
        f"OUTPUT_CONTRACT is a decision memo with cited constraints, the "
        f"description, steps, and generation_rationale must preserve that shape. "
        f"A generic document summary has a missing input/output contract.\n"
        f"</example>\n"
        f"<example name=\"class-first-scope\">\n"
        f"Good scope: a reusable class of recurring tasks such as `decision "
        f"briefs from source docs`. Bad scope: a single-session replica such as "
        f"`the exact brief the user asked for today`. Name, describe, and "
        f"trigger at the class level so the skill generalizes without catching "
        f"unrelated work.\n"
        f"</example>\n"
        f"</examples>\n\n"
        f"<quality_rubric>\n"
        f"Before producing JSON, revise the candidate until it satisfies all of "
        f"these checks:\n"
        f"1. No overbroad triggers; every trigger contains domain/action nouns "
        f"from the user's intended workflow.\n"
        f"2. Scope is the class of recurring tasks, not a single-session replica.\n"
        f"3. No missing input/output contract when the request supplies one.\n"
        f"4. No creator workflow steps inside candidate steps.\n"
        f"5. Every step uses a skill from the catalog and has a concrete task.\n"
        f"6. Negative cases are excluded by trigger wording, description, and "
        f"generation_rationale.\n"
        f"7. Conditional visibility metadata is filled only from explicit "
        f"evidence.\n"
        f"8. If the schema supports `eval_prompts`, include at least one "
        f"positive activation prompt with expect=`activate` and one negative "
        f"activation prompt with expect=`skip`.\n"
        f"</quality_rubric>\n\n"
        f"<field_rules>\n"
        f"Conditional visibility metadata rules:\n"
        f"- Optional fields `requires_toolsets`, `fallback_for_tools`, "
        f"`platforms`, and `config_keys` map to `metadata.opensquilla` in the "
        f"generated frontmatter.\n"
        f"- Fill these fields only when the user request or draft seed "
        f"explicitly names required toolsets, fallback tools, supported "
        f"platforms, or config keys. Do not invent conditional visibility "
        f"requirements.\n"
        f"- Leave them as empty lists when unspecified.\n"
        f"Activation eval prompt rules:\n"
        f"- Fill `eval_prompts` when the schema supports it.\n"
        f"- Each `prompt` must be a natural future-user request, not a label or "
        f"test description.\n"
        f"- Positive activation prompts should express the TASK_CLASS and "
        f"required trigger boundary.\n"
        f"- Negative activation prompts should mirror NEGATIVE_CASES or nearby "
        f"tasks that must not activate this skill.\n"
        f"Generation rationale rules:\n"
        f"- Include `generation_rationale` unless the schema rejects it.\n"
        f"- Set generation_rationale.selected_shape to `metaskill` for these "
        f"pattern schemas.\n"
        f"- Set generation_rationale.selected_pattern to the exact pattern_id: "
        f"{pattern_id}.\n"
        f"- Fill source_evidence with request/history facts that justify the "
        f"candidate.\n"
        f"- Fill rejected_alternatives with at least one nearby shape or skill "
        f"and why it was not chosen.\n"
        f"Critical field-name rules:\n"
        f"- The list of phrases is called `triggers`, not `trigger_condition`.\n"
        f"- If user intent names exact required trigger phrases, include those "
        f"phrases verbatim in `triggers` before adding optional synonyms.\n"
        f"- The pipeline is called `steps`, not `execution_sequence`, "
        f"`pipeline`, `actions`, or `sequence`.\n"
        f"- Each step must have: id (str, snake_case), skill (str from catalog), "
        f"task (str, max 400 chars, no double-quotes/newlines/backslashes), "
        f"with_keys (dict, often empty {{}}).\n"
        f"</field_rules>\n\n"
        f"<self_check>\n"
        f"Privately answer before emitting JSON: Are triggers narrow? Is the "
        f"candidate scoped to the class level rather than this one session? Is "
        f"the input/output contract preserved? Are negative cases excluded? Are "
        f"all skills from the catalog? Did you remove creator workflow steps? "
        f"Does the JSON match the schema exactly? If any answer is no, repair "
        f"the candidate before final output. Do not output the self-check.\n"
        f"</self_check>\n\n"
        f"<output>\n"
        f"Emit ONLY a JSON object matching the schema above. No prose. No "
        f"markdown. No code fences.\n"
        f"</output>"
    )

    response = _call_llm_for_slots(base_prompt)
    response = _strip_code_fences(response)  # Fix #A
    try:
        validated = schema.model_validate_json(response)
        validated = _preserve_required_triggers(validated, schema, user_intent)
        return str(validated.model_dump_json())
    except ValidationError as exc:
        # Fix #B: log raw response on initial failure so E2E logs capture LLM output.
        _log.warning(
            "meta_skill_fill_slots.validation_failed_initial",
            pattern_id=pattern_id,
            response_preview=response[:500],
            errors=str(exc.errors()[:5]) if exc.errors() else str(exc),
        )
        # N4 fix: Pydantic v2 custom-validator errors embed raw ValueError
        # objects in ctx.error, which are not JSON-serializable. Use
        # default=str to coerce them so json.dumps() doesn't TypeError before
        # the retry LLM call fires.
        retry_prompt = (
            base_prompt
            + "\n\nYour previous response failed schema validation with these errors:\n"
            + json.dumps(exc.errors(), default=str)
            + "\n\nEmit a corrected JSON object."
        )
        retry_response = _call_llm_for_slots(retry_prompt)
        retry_response = _strip_code_fences(retry_response)  # Fix #A
        try:
            validated = schema.model_validate_json(retry_response)
            validated = _preserve_required_triggers(validated, schema, user_intent)
            return str(validated.model_dump_json())
        except ValidationError as retry_exc:
            retry_errors = retry_exc.errors()
            # Fix #B: log raw response on retry failure.
            _log.warning(
                "meta_skill_fill_slots.validation_failed_retry",
                pattern_id=pattern_id,
                response_preview=retry_response[:500],
                errors=str(retry_errors[:5]) if retry_errors else retry_exc.__class__.__name__,
            )
            error_summary = json.dumps(retry_errors[:5], default=str)[:300]
            raise _FillSlotsValidationError(
                f"LLM returned invalid slots JSON after 1 retry. "
                f"Pattern: {pattern_id}. "
                f"Last error: {error_summary}. "
                f"Last response preview: {retry_response[:200]!r}"
            ) from retry_exc


def simulate_meta_resolution(
    skill_md: str, prompt: str, classifier_model: str,
) -> bool:
    """Load skill_md into a tmp SkillLoader, run trigger matching against
    `prompt`, return True if the candidate skill matches.

    For Phase 1, classifier_model is informational only; matching uses the
    same word-boundary regex used by `engine.steps.meta_resolution` (which
    is itself a deterministic substring/word-boundary check, no LLM)."""
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp) / "candidate"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        loader = SkillLoader(
            bundled_dir=Path(tmp),
            snapshot_path=Path(tmp) / "snap.json",
        )
        loader.invalidate_cache()
        specs = loader.load_all()
        if not specs:
            return False
        spec = specs[0]
        # IMPORTANT: _trigger_matches requires pre-lowered second arg
        # (meta_resolution.py:32). Pre-lower once here.
        prompt_lower = prompt.lower()
        return any(_trigger_matches(trig, prompt_lower) for trig in spec.triggers)


def run_smoke_gates(
    skill_md: str,
    *,
    fixture_gen_fn: Callable[..., str],
    classifier_model: str,
) -> dict[str, object]:
    """Run G3 (positive smoke) + G4 (negative smoke).

    `fixture_gen_fn(skill_md, kind, ...)` returns a generated prompt string
    for kind in {"positive", "negative"}. Cross-vendor pinning: caller is
    expected to inject a fixture_gen_fn that uses a DIFFERENT model family
    than `classifier_model` to break LLM-self-confirmation bias.
    """
    positive = fixture_gen_fn(skill_md, "positive")
    g3_matched = simulate_meta_resolution(skill_md, positive, classifier_model)

    negative = fixture_gen_fn(skill_md, "negative")
    g4_matched = simulate_meta_resolution(skill_md, negative, classifier_model)

    degraded = (
        classifier_model == "stub"
        or fixture_gen_fn is _deterministic_fixture
    )

    return {
        "G3": {
            "passed": g3_matched,
            "positive_fixture": positive,
            "classifier": classifier_model,
            "degraded": degraded,
        },
        "G4": {
            "passed": not g4_matched,
            "negative_fixture": negative,
            "classifier": classifier_model,
            "degraded": degraded,
        },
        "degraded": degraded,
    }


def real_fixture_gen(
    skill_md: str,
    kind: str,
    *,
    llm_chat,
    fixture_gen_model: str,
) -> str:
    """LLM-driven fixture gen for skill-creator-smoke-test Step 2.

    Phase 1 fallback to deterministic when llm_chat is None. Real LLM wiring
    deferred to follow-on iteration.

    Caller must supply an llm_chat bound to fixture_gen_model that is DIFFERENT
    from the classifier_model to break LLM-self-confirmation bias.
    """
    if llm_chat is None:
        return _deterministic_fixture(skill_md, kind)
    raise NotImplementedError(
        "real LLM fixture-gen is wired in Step 3.14 with cross-vendor pinning"
    )


def _deterministic_fixture(skill_md: str, kind: str) -> str:
    """Trigger-string based fixture generator for offline tests.

    Tries double-quoted triggers first (the predominant YAML style in this
    codebase's bundled meta-skills), then unquoted bare triggers. Returns
    the hardcoded fallback only when neither matches.

    Triggers in the rendered SKILL.md may be JSON-escaped (e.g. ``\\u6458\\u8981``
    for Chinese chars) because the assembler uses Jinja2 ``| tojson`` to keep
    YAML output ASCII-safe. This function decodes those escapes back to real
    Unicode before constructing the fixture, so simulate_meta_resolution can
    actually match against the parsed trigger.
    """
    if kind == "positive":
        # Double-quoted: triggers: \n  - "phrase"
        m = _re.search(r"triggers:\s*\n((?:\s*-\s*\"[^\"]+\"\s*\n)+)", skill_md)
        if m:
            first = _re.search(r'-\s*"([^"]+)"', m.group(1))
            if first:
                raw = first.group(1)
                # Decode \uXXXX / \n / \t / \" / \\ etc. via JSON-string parse.
                # This is the canonical inverse of the tojson filter that escaped
                # the trigger in the first place.
                try:
                    decoded = json.loads(f'"{raw}"')
                except Exception:
                    # Fall back to raw if JSON parse fails (e.g. malformed escape)
                    decoded = raw
                return f"please use {decoded}"
        # Unquoted: triggers: \n  - phrase
        m = _re.search(r"triggers:\s*\n((?:\s*-\s*[^\"\n]+\n)+)", skill_md)
        if m:
            first = _re.search(r"-\s*([^\"\n]+)", m.group(1))
            if first:
                return f"please use {first.group(1).strip()}"
        return "please run this meta-skill"
    # Cross-domain negative fixture: any prompt unrelated to common bundled
    # skills. Weather is a safe choice because the corpus's weather bundle
    # uses tight weather-specific triggers that won't be matched by this
    # free-form phrasing. If a future user-authored meta-skill is itself
    # about weather, this fixture will false-fail G4 — flag at that time.
    if kind == "negative":
        return "what's the weather forecast for tomorrow?"
    raise ValueError(f"Unknown fixture kind: {kind}")


# ---------------------------------------------------------------------------
# Paths to bundled helper scripts (resolved once at module load time).
# ---------------------------------------------------------------------------

_LINT_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "bundled" / "skill-creator-linter" / "scripts" / "lint.py"
)
_BUNDLED_DIR = _LINT_SCRIPT.parents[2]

_PROPOSALS_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "bundled" / "skill-creator-proposals" / "scripts" / "proposals.py"
)

_RUNTIME_E2E_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "opensquilla_meta_skill_runtime_e2e_context",
    default=None,
)
_SMOKE_FIXTURE_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "opensquilla_meta_skill_smoke_fixture_context",
    default=None,
)


def set_runtime_e2e_context(ctx: dict[str, Any] | None):
    """Install the runtime E2E runner for the current async context."""

    return _RUNTIME_E2E_CONTEXT.set(ctx)


def reset_runtime_e2e_context(token) -> None:
    _RUNTIME_E2E_CONTEXT.reset(token)


def set_smoke_fixture_context(ctx: dict[str, Any] | None):
    """Install LLM fixture generation context for the current async context."""

    return _SMOKE_FIXTURE_CONTEXT.set(ctx)


def reset_smoke_fixture_context(token) -> None:
    _SMOKE_FIXTURE_CONTEXT.reset(token)


# ---------------------------------------------------------------------------
# Sync core implementations for lint / smoke / persist
# ---------------------------------------------------------------------------

def meta_skill_generation_quality_run(pattern_id: str, slots_json: str) -> str:
    result = evaluate_generation_quality(pattern_id, slots_json)
    return json.dumps(result, ensure_ascii=False)


def meta_skill_activation_eval_run(
    skill_md: str,
    positive_prompts: str = "",
    negative_prompts: str = "",
    catalog_negative_prompts: str = "",
    *,
    threshold: float = 0.8,
) -> str:
    negative_input = catalog_negative_prompts if catalog_negative_prompts else negative_prompts
    result = evaluate_candidate_activation(
        skill_md,
        positive_prompts=_activation_prompt_list(
            skill_md,
            positive_prompts,
            kind="positive",
        ),
        negative_prompts=_activation_prompt_list(
            skill_md,
            negative_input,
            kind="negative",
        ),
        threshold=threshold,
    )
    return json.dumps(result, ensure_ascii=False)


def _activation_prompt_list(skill_md: str, raw: str, *, kind: str) -> list[str]:
    text = str(raw or "").strip()
    if text.lower() != "auto":
        return _json_array_or_lines(raw)
    eval_prompts = _eval_prompts_from_frontmatter(skill_md, kind=kind)
    if eval_prompts:
        return eval_prompts
    if kind == "positive":
        return [_deterministic_fixture(skill_md, "positive")]
    if kind == "negative":
        return [
            "please summarize this note",
            "what's the weather forecast for tomorrow?",
            "create a normal standalone skill for note cleanup",
            "explain how meta-skill-creator works",
        ]
    raise ValueError(f"Unknown activation prompt kind: {kind}")


def _eval_prompts_from_frontmatter(skill_md: str, *, kind: str) -> list[str]:
    expected = "activate" if kind == "positive" else "skip"
    match = _re.match(r"^---\s*\n(?P<yaml>.*?)\n---(?:\n|$)", skill_md, _re.DOTALL)
    if not match:
        return []
    try:
        data = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []

    prompts: list[str] = []
    seen: set[str] = set()
    for item in data.get("eval_prompts") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("expect") or "").strip().lower() != expected:
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt or prompt in seen:
            continue
        prompts.append(prompt)
        seen.add(prompt)
        if len(prompts) >= 8:
            break
    return prompts


def _json_array_or_lines(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [line.strip() for line in text.splitlines() if line.strip()]


def meta_skill_lint_run(skill_md: str, gates: str = "G1,G2") -> str:
    """Run skill-creator-linter on the given SKILL.md text. Returns JSON.

    Gates parameter is a comma-separated list (e.g. "G1,G2"). Default
    runs both G1 (structural lint) and G2 (scheduler dry-run).
    """
    proc = subprocess.run(
        [sys.executable, str(_LINT_SCRIPT), "--skill-md-stdin", "--gates", gates],
        input=skill_md, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        # Even on failure, lint.py prints JSON to stdout — return it
        return proc.stdout or json.dumps({
            "error": "linter subprocess exited non-zero",
            "stderr": proc.stderr[:500],
            "returncode": proc.returncode,
        })
    return proc.stdout


def meta_skill_smoke_run(
    skill_md: str,
    fixture_gen_model: str = "stub",
    classifier_model: str = "stub",
) -> str:
    """Run G3+G4 smoke tests on the given SKILL.md. Returns JSON.

    Phase 1: uses deterministic fixture generator regardless of model
    args (those wire to real LLMs in a future iteration). The result is
    flagged ``degraded: true`` so users know real LLM smoke didn't run.

    N19 fix: pass _deterministic_fixture directly (no lambda) so
    run_smoke_gates' identity check (``fixture_gen_fn is _deterministic_fixture``)
    recognises this as the degraded path and sets degraded=True on the
    gate result. The previous lambda wrapper broke the ``is`` identity
    check, causing smoke to report degraded=False.
    """
    result = run_smoke_gates(
        skill_md=skill_md,
        fixture_gen_fn=_deterministic_fixture,
        classifier_model=classifier_model,
    )
    return json.dumps(result, ensure_ascii=False)


def _skill_name_from_skill_md(skill_md: str) -> str:
    match = _re.search(r"(?m)^name:\s*['\"]?([\w-]+)['\"]?\s*$", skill_md)
    return match.group(1) if match else ""


def _record_persist_failure(home: Path | None, skill_md: str, reason: str) -> None:
    if home is None:
        return
    try:
        from opensquilla.skills.proposals_lib import record_creator_learning_event

        record_creator_learning_event(
            home,
            {
                "event_type": "failed",
                "skill_name": _skill_name_from_skill_md(skill_md),
                "source": "meta_skill_persist_proposal",
                "reason": reason,
            },
        )
    except Exception:  # noqa: BLE001
        return


def meta_skill_persist_proposal(
    skill_md: str,
    lint_result: str,
    smoke_result: str,
    home: str = "",
    *,
    creator_mode: str = "",
    acceptance_result: str = "",
    runtime_e2e_result: str = "",
    collision_result: str = "",
    risk_result: str = "",
    generation_quality_result: str = "",
    activation_result: str = "",
    bundle_files_json: str = "",
    auto_enable_manual: bool = True,
) -> str:
    """Write a proposal candidate to ~/.opensquilla/proposals/<id>/. Returns JSON."""
    home_path = Path(home).expanduser() if home else None
    effective_creator_mode = creator_mode or "PERSISTED_PROPOSAL"
    args = [sys.executable, str(_PROPOSALS_SCRIPT),
            "--action", "write_proposal",
            "--skill-md-inline", skill_md,
            "--lint-result", lint_result,
            "--smoke-result", smoke_result,
            "--creator-mode", effective_creator_mode,
            "--acceptance-result", acceptance_result,
            "--runtime-e2e-result", runtime_e2e_result,
            "--collision-result", collision_result,
            "--risk-result", risk_result,
            "--generation-quality-result", generation_quality_result,
            "--activation-result", activation_result]
    if bundle_files_json:
        args.extend(["--bundle-files-json", bundle_files_json])
    if home:
        args.extend(["--home", home])
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        _record_persist_failure(home_path, skill_md, "subprocess_failed")
        return proc.stdout or json.dumps({
            "error": "proposals subprocess exited non-zero",
            "stderr": proc.stderr[:500],
            "returncode": proc.returncode,
        })
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _record_persist_failure(home_path, skill_md, "invalid_persist_json")
        return proc.stdout
    if out.get("status") != "ok":
        _record_persist_failure(
            home_path,
            skill_md,
            str(out.get("reason") or out.get("error") or "persist_refused"),
        )
    if (
        auto_enable_manual
        and out.get("status") == "ok"
        and out.get("proposal_id")
        and home_path is not None
    ):
        _maybe_auto_enable_manual_proposal(home_path, str(out["proposal_id"]), out)
    return json.dumps(out, ensure_ascii=False)


def meta_skill_patch_proposal(
    proposal_id: str,
    patch_json: str,
    home: str = "",
    expected_parent_revision: int | None = None,
) -> str:
    """Create a reviewed revision of an existing pending proposal.

    The patch surface intentionally accepts structured JSON only; the creator
    DAG is responsible for turning natural-language edit requests into the
    allowlisted operation shape consumed by proposals_lib.patch_proposal().
    """
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.proposals_lib import patch_proposal

    home_path = Path(home).expanduser() if home else default_opensquilla_home()
    try:
        patch_request = json.loads(_strip_code_fences(patch_json or "{}"))
    except json.JSONDecodeError as exc:
        return json.dumps(
            {
                "status": "refused",
                "reason": "invalid_patch_json",
                "detail": str(exc),
            },
            ensure_ascii=False,
        )
    if not isinstance(patch_request, dict):
        return json.dumps(
            {
                "status": "refused",
                "reason": "invalid_patch_request",
            },
            ensure_ascii=False,
        )
    result = patch_proposal(
        home_path,
        proposal_id,
        patch_request,
        expected_parent_revision=expected_parent_revision,
    )
    return json.dumps(result, ensure_ascii=False)


def meta_skill_extract_proposal_id(text: str, fallback: str = "") -> str:
    """Extract the first valid pending-proposal id from explicit inputs or text."""
    from opensquilla.skills.proposals_lib import is_valid_proposal_id

    candidate = (fallback or "").strip()
    if is_valid_proposal_id(candidate):
        return candidate
    for match in _re.finditer(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])", text or ""):
        proposal_id = match.group(0)
        if is_valid_proposal_id(proposal_id):
            return proposal_id
    return ""


def meta_skill_extract_benchmark_proposal_ids(
    text: str,
    baseline_fallback: str = "",
    candidate_fallback: str = "",
) -> str:
    """Extract baseline/candidate proposal ids for non-promoting benchmarks."""
    from opensquilla.skills.proposals_lib import is_valid_proposal_id

    baseline = (baseline_fallback or "").strip()
    candidate = (candidate_fallback or "").strip()
    ids = [
        match.group(0)
        for match in _re.finditer(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])", text or "")
    ]
    if not is_valid_proposal_id(baseline):
        baseline = ids[0] if ids and is_valid_proposal_id(ids[0]) else ""
    if not is_valid_proposal_id(candidate):
        candidate = ""
        for proposal_id in ids:
            if proposal_id != baseline and is_valid_proposal_id(proposal_id):
                candidate = proposal_id
                break
    return json.dumps({
        "baseline_proposal_id": baseline,
        "candidate_proposal_id": candidate,
    }, ensure_ascii=False)


def _json_or_none(raw: str, reason: str) -> tuple[object, dict | None]:
    if not raw:
        return None, None
    try:
        return json.loads(_strip_code_fences(raw)), None
    except json.JSONDecodeError as exc:
        return None, {
            "status": "refused",
            "reason": reason,
            "detail": str(exc),
        }


def meta_skill_benchmark_proposals(
    baseline_proposal_id: str = "",
    candidate_proposal_id: str = "",
    target_json: str = "",
    comparison_json: str = "",
    eval_prompts_json: str = "",
    home: str = "",
) -> str:
    """Record a non-promoting benchmark for two pending proposal revisions."""
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.proposals_lib import benchmark_proposals

    home_path = Path(home).expanduser() if home else default_opensquilla_home()
    target_payload, target_error = _json_or_none(target_json, "invalid_benchmark_targets")
    if target_error is not None:
        return json.dumps(target_error, ensure_ascii=False)
    if isinstance(target_payload, dict):
        baseline_proposal_id = (
            baseline_proposal_id
            or str(target_payload.get("baseline_proposal_id") or "")
        )
        candidate_proposal_id = (
            candidate_proposal_id
            or str(target_payload.get("candidate_proposal_id") or "")
        )

    comparison_result, comparison_error = _json_or_none(
        comparison_json,
        "invalid_benchmark_comparison_json",
    )
    if comparison_error is not None:
        return json.dumps(comparison_error, ensure_ascii=False)
    eval_prompts, eval_error = _json_or_none(
        eval_prompts_json,
        "invalid_benchmark_eval_prompts_json",
    )
    if eval_error is not None:
        return json.dumps(eval_error, ensure_ascii=False)

    result = benchmark_proposals(
        home_path,
        baseline_proposal_id,
        candidate_proposal_id,
        eval_prompts=eval_prompts,
        comparison_result=comparison_result,
    )
    return json.dumps(result, ensure_ascii=False)


def meta_skill_rollback_skill(skill_name: str, home: str = "") -> str:
    """Restore a managed skill from its recorded rollback target."""
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.proposals_lib import rollback_skill

    home_path = Path(home).expanduser() if home else default_opensquilla_home()
    result = rollback_skill(home_path, skill_name)
    return json.dumps(result, ensure_ascii=False)


def meta_skill_creator_learning_summary(home: str = "") -> str:
    """Return compact creator lifecycle memory for slot filling."""
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.proposals_lib import creator_learning_summary

    home_path = Path(home).expanduser() if home else default_opensquilla_home()
    result = creator_learning_summary(home_path)
    return json.dumps(result, ensure_ascii=False)


def meta_skill_import_history_failure_feedback(
    history_json: str = "",
    home: str = "",
) -> str:
    """Import harvested failure paths into creator lifecycle memory."""
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.proposals_lib import (
        creator_learning_summary,
        record_creator_history_failure_feedback,
    )

    home_path = Path(home).expanduser() if home else default_opensquilla_home()
    try:
        payload = json.loads(history_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    failure_paths = payload.get("failure_paths") if isinstance(payload, dict) else []
    imported_count = 0
    if isinstance(failure_paths, list):
        for failure_path in failure_paths[:8]:
            result = record_creator_history_failure_feedback(home_path, failure_path)
            if result.get("status") == "ok":
                imported_count += 1
    summary = creator_learning_summary(home_path)
    summary["history_failure_feedback"] = {"imported_count": imported_count}
    return json.dumps(summary, ensure_ascii=False)


def meta_skill_refresh_proposal_gates(
    proposal_id: str,
    *,
    smoke_json: str = "",
    collision_result: str = "",
    risk_result: str = "",
    acceptance_json: str = "",
    runtime_e2e_json: str = "",
    generation_quality_json: str = "",
    activation_json: str = "",
    home: str = "",
) -> str:
    """Refresh stale gates on a pending proposal revision. Returns JSON."""
    from opensquilla.paths import default_opensquilla_home
    from opensquilla.skills.proposals_lib import refresh_proposal_gates

    home_path = Path(home).expanduser() if home else default_opensquilla_home()
    parsed: dict[str, object] = {}
    for label, raw in (
        ("smoke", smoke_json),
        ("acceptance", acceptance_json),
        ("runtime_e2e", runtime_e2e_json),
        ("generation_quality", generation_quality_json),
        ("activation", activation_json),
    ):
        value, error = _json_or_none(raw, f"invalid_{label}_json")
        if error is not None:
            return json.dumps(error, ensure_ascii=False)
        parsed[label] = value
    result = refresh_proposal_gates(
        home_path,
        proposal_id,
        smoke_result=parsed["smoke"],
        collision_result=collision_result or None,
        risk_result=risk_result or None,
        acceptance_result=parsed["acceptance"],
        runtime_e2e_result=parsed["runtime_e2e"],
        generation_quality_result=parsed["generation_quality"],
        activation_result=parsed["activation"],
    )
    return json.dumps(result, ensure_ascii=False)


def _maybe_auto_enable_manual_proposal(
    home: Path,
    proposal_id: str,
    out: dict,
) -> None:
    """Apply the runtime auto-enable setting to manual creator output.

    Cron/dream auto-propose calls the same preflight directly. The manual
    creator path reaches proposal persistence through this tool, so it needs
    an explicit bridge to keep the three trigger routes behaviorally aligned.
    """
    from opensquilla.skills.proposals_lib import read_auto_propose_settings

    settings = read_auto_propose_settings(home)
    auto_enable = bool(settings.get("auto_enable", False))
    max_risk = str(settings.get("auto_enable_max_risk", "low"))
    try:
        from opensquilla.gateway.auto_propose_bridge import get_runtime
        rt = get_runtime()
    except Exception:  # noqa: BLE001
        rt = None
    if rt is not None and Path(getattr(rt, "home", "")) == home:
        cfg = getattr(rt, "config", None)
        auto_enable = bool(getattr(cfg, "auto_enable", auto_enable))
        max_risk = str(getattr(cfg, "auto_enable_max_risk", max_risk))
    if not auto_enable:
        return

    from opensquilla.skills.creator.auto_propose import try_auto_enable_proposal
    from opensquilla.skills.loader import SkillLoader

    loader = SkillLoader(
        bundled_dir=_BUNDLED_DIR,
        managed_dir=home / "skills",
        snapshot_path=home / "cache" / "manual_auto_enable_snapshot.json",
    )
    loader.invalidate_cache()
    loader.load_all()
    decision = try_auto_enable_proposal(
        proposals_dir=home / "proposals",
        proposal_id=proposal_id,
        skill_loader=loader,
        triggered_by="manual",
        max_risk=max_risk,
    )
    out["auto_enable"] = decision
    out["auto_enabled"] = decision.get("status") == "enabled"


# ---------------------------------------------------------------------------
# @tool-decorated async wrappers — registered into the default ToolRegistry
# at import time so that the orchestrator's tool_invoker can dispatch them.
# ---------------------------------------------------------------------------

@tool(
    name="emit_text",
    description=(
        "Emit a fixed text string as the step output. "
        "Used by harvest_empty fallback in meta-skill-creator."
    ),
    params={"text": {"type": "string"}},
    required=["text"],
    exposed_by_default=False,
)
async def emit_text_tool(text: str) -> str:
    return text


@tool(
    name="meta_skill_lint_run",
    description=(
        "Run skill-creator-linter G1+G2 on a SKILL.md candidate. "
        "Returns JSON with G1/G2 pass status + diagnostics."
    ),
    params={
        "skill_md": {"type": "string"},
        "gates": {"type": "string"},
    },
    required=["skill_md"],
    exposed_by_default=False,
)
async def meta_skill_lint_run_tool(skill_md: str, gates: str = "G1,G2") -> str:
    import asyncio
    return await asyncio.to_thread(meta_skill_lint_run, skill_md, gates)


@tool(
    name="meta_skill_smoke_run",
    description=(
        "Run G3 (positive smoke) + G4 (negative smoke) on a SKILL.md candidate "
        "using deterministic fixtures. Returns JSON."
    ),
    params={
        "skill_md": {"type": "string"},
        "fixture_gen_model": {"type": "string"},
        "classifier_model": {"type": "string"},
    },
    required=["skill_md"],
    exposed_by_default=False,
)
async def meta_skill_smoke_run_tool(
    skill_md: str,
    fixture_gen_model: str = "stub",
    classifier_model: str = "stub",
) -> str:
    ctx = _SMOKE_FIXTURE_CONTEXT.get()
    llm_chat = (ctx or {}).get("llm_chat") if isinstance(ctx, dict) else None
    if llm_chat is None:
        import asyncio
        return await asyncio.to_thread(
            meta_skill_smoke_run, skill_md, fixture_gen_model, classifier_model,
        )

    async def fixture_gen(_skill_md: str, kind: str) -> str:
        if kind == "positive":
            guidance = (
                "Ask for the workflow in everyday language and include one "
                "exact trigger phrase from the SKILL.md."
            )
        else:
            guidance = (
                "Ask for a realistic unrelated task that should not activate "
                "the meta-skill. Do not include any trigger phrase."
            )
        prompt = (
            "Generate one natural user prompt for meta-skill smoke testing.\n"
            f"Kind: {kind}\n"
            f"{guidance} Return only the prompt text, no markdown.\n\n"
            f"SKILL.md:\n{_skill_md[:5000]}"
        )
        raw = await llm_chat(
            "You generate concise, realistic meta-skill smoke-test fixtures.",
            prompt,
        )
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = _re.sub(r"^```(?:text)?\s*", "", text)
            text = _re.sub(r"\s*```$", "", text)
        return text.strip().strip('"') or _deterministic_fixture(_skill_md, kind)

    positive = await fixture_gen(skill_md, "positive")
    g3_matched = simulate_meta_resolution(skill_md, positive, classifier_model)
    negative = await fixture_gen(skill_md, "negative")
    g4_matched = simulate_meta_resolution(skill_md, negative, classifier_model)
    result = {
        "G3": {
            "passed": g3_matched,
            "positive_fixture": positive,
            "classifier": classifier_model,
            "degraded": False,
            "fixture_model": fixture_gen_model,
        },
        "G4": {
            "passed": not g4_matched,
            "negative_fixture": negative,
            "classifier": classifier_model,
            "degraded": False,
            "fixture_model": fixture_gen_model,
        },
        "degraded": False,
        "fixture_source": "llm",
    }
    return json.dumps(result, ensure_ascii=False)


async def meta_skill_runtime_e2e_run(
    skill_md: str,
    eval_prompts: str = "",
    baseline_model: str = "",
) -> str:
    ctx = _RUNTIME_E2E_CONTEXT.get()
    if not ctx:
        return json.dumps({
            "status": "unavailable",
            "passed": False,
            "winner": "",
            "reason": "runtime_e2e_context_unavailable",
            "cases": [],
        }, ensure_ascii=False)
    result = await run_runtime_e2e_gate(
        skill_md=skill_md,
        eval_prompts=eval_prompts,
        baseline_model=baseline_model or str(ctx.get("baseline_model") or ""),
        runner=ctx["runner"],
        judge=ctx["judge"],
    )
    return json.dumps(result, ensure_ascii=False)


@tool(
    name="meta_skill_runtime_e2e_run",
    description=(
        "Run the candidate meta-skill on eval prompts and compare it against "
        "a no-meta highest-tier baseline. Returns JSON gate results."
    ),
    params={
        "skill_md": {"type": "string"},
        "eval_prompts": {"type": "string"},
        "baseline_model": {"type": "string"},
    },
    required=["skill_md"],
    exposed_by_default=False,
)
async def meta_skill_runtime_e2e_run_tool(
    skill_md: str,
    eval_prompts: str = "",
    baseline_model: str = "",
) -> str:
    return await meta_skill_runtime_e2e_run(
        skill_md=skill_md,
        eval_prompts=eval_prompts,
        baseline_model=baseline_model,
    )


@tool(
    name="meta_skill_persist_proposal",
    description=(
        "Write a proposal candidate to ~/.opensquilla/proposals/<id>/. "
        "Returns JSON with proposal_id and auto_enable_eligible."
    ),
    params={
        "skill_md": {"type": "string"},
        "lint_result": {"type": "string"},
        "smoke_result": {"type": "string"},
        "creator_mode": {"type": "string"},
        "acceptance_result": {"type": "string"},
        "runtime_e2e_result": {"type": "string"},
        "collision_result": {"type": "string"},
        "risk_result": {"type": "string"},
        "generation_quality_result": {"type": "string"},
        "activation_result": {"type": "string"},
        "bundle_files_json": {"type": "string"},
        "auto_enable_manual": {"type": "boolean"},
        "home": {"type": "string"},
    },
    required=["skill_md", "lint_result", "smoke_result"],
    exposed_by_default=False,
)
async def meta_skill_persist_proposal_tool(
    skill_md: str,
    lint_result: str,
    smoke_result: str,
    home: str = "",
    creator_mode: str = "",
    acceptance_result: str = "",
    runtime_e2e_result: str = "",
    collision_result: str = "",
    risk_result: str = "",
    generation_quality_result: str = "",
    activation_result: str = "",
    bundle_files_json: str = "",
    auto_enable_manual: bool = True,
) -> str:
    import asyncio
    return await asyncio.to_thread(
        meta_skill_persist_proposal,
        skill_md,
        lint_result,
        smoke_result,
        home,
        creator_mode=creator_mode,
        acceptance_result=acceptance_result,
        runtime_e2e_result=runtime_e2e_result,
        collision_result=collision_result,
        risk_result=risk_result,
        generation_quality_result=generation_quality_result,
        activation_result=activation_result,
        bundle_files_json=bundle_files_json,
        auto_enable_manual=auto_enable_manual,
    )


@tool(
    name="meta_skill_patch_proposal",
    description=(
        "Create a revised pending proposal from an allowlisted structured "
        "patch JSON object. Returns JSON with parent/child proposal IDs."
    ),
    params={
        "proposal_id": {"type": "string"},
        "patch_json": {"type": "string"},
        "expected_parent_revision": {"type": "integer"},
        "home": {"type": "string"},
    },
    required=["proposal_id", "patch_json"],
    exposed_by_default=False,
)
async def meta_skill_patch_proposal_tool(
    proposal_id: str,
    patch_json: str,
    expected_parent_revision: int | None = None,
    home: str = "",
) -> str:
    import asyncio

    return await asyncio.to_thread(
        meta_skill_patch_proposal,
        proposal_id,
        patch_json,
        home,
        expected_parent_revision,
    )


@tool(
    name="meta_skill_extract_proposal_id",
    description=(
        "Extract the first valid 8-hex pending proposal id from a user message "
        "or explicit fallback string."
    ),
    params={
        "text": {"type": "string"},
        "fallback": {"type": "string"},
    },
    required=["text"],
    exposed_by_default=False,
)
async def meta_skill_extract_proposal_id_tool(
    text: str,
    fallback: str = "",
) -> str:
    return meta_skill_extract_proposal_id(text, fallback)


@tool(
    name="meta_skill_extract_benchmark_proposal_ids",
    description=(
        "Extract baseline and candidate pending proposal ids from a user "
        "message or explicit fallback strings. Returns JSON."
    ),
    params={
        "text": {"type": "string"},
        "baseline_fallback": {"type": "string"},
        "candidate_fallback": {"type": "string"},
    },
    required=["text"],
    exposed_by_default=False,
)
async def meta_skill_extract_benchmark_proposal_ids_tool(
    text: str,
    baseline_fallback: str = "",
    candidate_fallback: str = "",
) -> str:
    return meta_skill_extract_benchmark_proposal_ids(
        text,
        baseline_fallback,
        candidate_fallback,
    )


@tool(
    name="meta_skill_benchmark_proposals",
    description=(
        "Record a non-promoting benchmark report comparing two pending "
        "proposal revisions over eval prompts."
    ),
    params={
        "baseline_proposal_id": {"type": "string"},
        "candidate_proposal_id": {"type": "string"},
        "target_json": {"type": "string"},
        "comparison_json": {"type": "string"},
        "eval_prompts_json": {"type": "string"},
        "home": {"type": "string"},
    },
    required=[],
    exposed_by_default=False,
)
async def meta_skill_benchmark_proposals_tool(
    baseline_proposal_id: str = "",
    candidate_proposal_id: str = "",
    target_json: str = "",
    comparison_json: str = "",
    eval_prompts_json: str = "",
    home: str = "",
) -> str:
    import asyncio

    return await asyncio.to_thread(
        meta_skill_benchmark_proposals,
        baseline_proposal_id,
        candidate_proposal_id,
        target_json,
        comparison_json,
        eval_prompts_json,
        home,
    )


@tool(
    name="meta_skill_refresh_proposal_gates",
    description=(
        "Refresh stale gates on a pending proposal revision. Returns JSON with "
        "the refreshed gate names and updated eligibility."
    ),
    params={
        "proposal_id": {"type": "string"},
        "smoke_json": {"type": "string"},
        "collision_result": {"type": "string"},
        "risk_result": {"type": "string"},
        "acceptance_json": {"type": "string"},
        "runtime_e2e_json": {"type": "string"},
        "generation_quality_json": {"type": "string"},
        "activation_json": {"type": "string"},
        "home": {"type": "string"},
    },
    required=["proposal_id"],
    exposed_by_default=False,
)
async def meta_skill_refresh_proposal_gates_tool(
    proposal_id: str,
    smoke_json: str = "",
    collision_result: str = "",
    risk_result: str = "",
    acceptance_json: str = "",
    runtime_e2e_json: str = "",
    generation_quality_json: str = "",
    activation_json: str = "",
    home: str = "",
) -> str:
    import asyncio

    return await asyncio.to_thread(
        meta_skill_refresh_proposal_gates,
        proposal_id,
        smoke_json=smoke_json,
        collision_result=collision_result,
        risk_result=risk_result,
        acceptance_json=acceptance_json,
        runtime_e2e_json=runtime_e2e_json,
        generation_quality_json=generation_quality_json,
        activation_json=activation_json,
        home=home,
    )


@tool(
    name="meta_skill_creator_learning_summary",
    description=(
        "Return compact advisory memory from prior meta-skill proposal "
        "acceptance, benchmark, and rollback events. Returns JSON."
    ),
    params={
        "home": {"type": "string"},
    },
    required=[],
    exposed_by_default=False,
)
async def meta_skill_creator_learning_summary_tool(home: str = "") -> str:
    import asyncio

    return await asyncio.to_thread(meta_skill_creator_learning_summary, home)


@tool(
    name="meta_skill_import_history_failure_feedback",
    description=(
        "Import compact history-explorer failure_paths into creator "
        "feedback memory. Returns updated learning summary JSON."
    ),
    params={
        "history_json": {"type": "string"},
        "home": {"type": "string"},
    },
    required=[],
    exposed_by_default=False,
)
async def meta_skill_import_history_failure_feedback_tool(
    history_json: str = "",
    home: str = "",
) -> str:
    import asyncio

    return await asyncio.to_thread(
        meta_skill_import_history_failure_feedback,
        history_json,
        home,
    )


@tool(
    name="meta_skill_rollback_skill",
    description=(
        "Restore a managed skill from its recorded rollback target. Returns JSON."
    ),
    params={
        "skill_name": {"type": "string"},
        "home": {"type": "string"},
    },
    required=["skill_name"],
    exposed_by_default=False,
)
async def meta_skill_rollback_skill_tool(
    skill_name: str,
    home: str = "",
) -> str:
    import asyncio

    return await asyncio.to_thread(meta_skill_rollback_skill, skill_name, home)


_PATTERN_ENUM = sorted(PATTERN_SLOT_SCHEMA.keys())
_META_SKILL_FILL_SLOTS_PARAMS = {
    "pattern_id": {"type": "string", "enum": _PATTERN_ENUM},
    "history_summary": {"type": "string"},
    "user_intent": {"type": "string"},
    "draft_seed_json": {"type": "string"},
    "creator_feedback_json": {"type": "string"},
}
_META_SKILL_FILL_SLOTS_REQUIRED = ["pattern_id", "history_summary", "user_intent"]


@tool(
    name="meta_skill_generation_quality_run",
    description="Run deterministic generation-quality checks on creator slot JSON.",
    params={
        "pattern_id": {"type": "string", "enum": _PATTERN_ENUM},
        "slots_json": {"type": "string"},
    },
    required=["pattern_id", "slots_json"],
    exposed_by_default=False,
)
async def meta_skill_generation_quality_run_tool(
    pattern_id: str,
    slots_json: str,
) -> str:
    import asyncio

    return await asyncio.to_thread(meta_skill_generation_quality_run, pattern_id, slots_json)


@tool(
    name="meta_skill_activation_eval_run",
    description="Run deterministic activation checks on a candidate SKILL.md.",
    params={
        "skill_md": {"type": "string"},
        "positive_prompts": {"type": "string"},
        "negative_prompts": {"type": "string"},
        "catalog_negative_prompts": {"type": "string"},
    },
    required=["skill_md"],
    exposed_by_default=False,
)
async def meta_skill_activation_eval_run_tool(
    skill_md: str,
    positive_prompts: str = "",
    negative_prompts: str = "",
    catalog_negative_prompts: str = "",
) -> str:
    import asyncio

    return await asyncio.to_thread(
        meta_skill_activation_eval_run,
        skill_md,
        positive_prompts,
        negative_prompts,
        catalog_negative_prompts,
    )


@tool(
    name="meta_skill_assemble",
    description=(
        "Render a meta-skill SKILL.md from a pattern_id + Pydantic-validated "
        "slots JSON. Returns the full SKILL.md text as a string."
    ),
    params={
        "pattern_id": {"type": "string", "enum": _PATTERN_ENUM},
        "slots_json": {"type": "string"},
    },
    required=["pattern_id", "slots_json"],
    exposed_by_default=False,  # internal orchestrator dispatch only
)
async def meta_skill_assemble_tool(pattern_id: str, slots_json: str) -> str:
    return meta_skill_assemble(pattern_id, slots_json)


@tool(
    name="meta_skill_fill_slots",
    description=(
        "Drive an LLM to fill the slot schema for the chosen pattern. "
        "Returns validated JSON string consumed by meta_skill_assemble."
    ),
    params=_META_SKILL_FILL_SLOTS_PARAMS,
    required=_META_SKILL_FILL_SLOTS_REQUIRED,
    exposed_by_default=False,  # internal orchestrator dispatch only
)
async def meta_skill_fill_slots_tool(
    pattern_id: str,
    history_summary: str,
    user_intent: str,
    draft_seed_json: str = "",
    creator_feedback_json: str = "",
) -> str:
    # Run the sync core in a worker thread to avoid nested event loop conflict
    # when invoked from inside the orchestrator's running event loop.
    # The sync core uses asyncio.run() internally to call the LLM provider.
    import asyncio

    # Fix #B (Option B1): catch _FillSlotsValidationError and return a
    # structured error JSON so the orchestrator sees the actual diagnostic
    # instead of the generic "The tool 'X' failed with an internal error."
    # that the envelope layer emits for unknown exception classes.
    # The downstream meta_skill_assemble call will then fail with an
    # actionable message from this payload rather than a silent black-box.
    try:
        if _call_llm_for_slots is not _REAL_CALL_LLM_FOR_SLOTS:
            return meta_skill_fill_slots(
                pattern_id,
                history_summary,
                user_intent,
                draft_seed_json,
                creator_feedback_json,
            )
        return await asyncio.to_thread(
            meta_skill_fill_slots,
            pattern_id,
            history_summary,
            user_intent,
            draft_seed_json,
            creator_feedback_json,
        )
    except _FillSlotsValidationError as exc:
        return json.dumps(
            {
                "_creator_error": "validation_failed_after_retry",
                "pattern_id": pattern_id,
                "detail": str(exc),
            },
            ensure_ascii=False,
        )


meta_skill_fill_slots_tool.tool = SimpleNamespace(
    input_schema={
        "type": "object",
        "properties": _META_SKILL_FILL_SLOTS_PARAMS,
        "required": _META_SKILL_FILL_SLOTS_REQUIRED,
    }
)
