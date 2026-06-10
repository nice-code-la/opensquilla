"""Tests for creator/proposer.py (fill_slots, assemble) + patterns."""

from __future__ import annotations

import json

import pytest

from opensquilla.skills.creator.patterns.schemas import (
    FanOutMergeSlots,
    SequentialSlots,
)
from opensquilla.skills.creator.proposer import meta_skill_assemble


def test_sequential_slots_min_steps() -> None:
    with pytest.raises(ValueError):
        SequentialSlots(
            name="test-x", description="d" * 30, triggers=["t"],
            steps=[{"id": "a", "skill": "x", "task": "t"}],
        )


def test_sequential_with_keys_default_empty() -> None:
    slots = SequentialSlots(
        name="test-x", description="d" * 30, triggers=["t"],
        steps=[
            {"id": "a", "skill": "summarize", "task": "do thing"},
            {"id": "b", "skill": "memory", "task": "save"},
        ],
    )
    assert slots.steps[0].with_keys == {}


def test_fanout_tail_optional() -> None:
    slots = FanOutMergeSlots(
        name="test-x", description="d" * 30, triggers=["t"],
        branches=[
            {"id": "a", "skill": "weather", "task": "t"},
            {"id": "b", "skill": "summarize", "task": "t"},
        ],
        merge={"id": "m", "skill": "summarize", "task": "t"},
    )
    assert slots.tail is None


def test_meta_skill_assemble_p1() -> None:
    slots = {
        "name": "test-t1", "description": "d" * 30, "triggers": ["go"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "extract", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "store", "with_keys": {}},
        ],
        "meta_priority": 50,
    }
    md = meta_skill_assemble("p1_sequential", json.dumps(slots))
    # N2: tojson wraps values in JSON double-quotes (valid YAML scalars)
    assert 'name: "test-t1"' in md
    assert 'skill: "summarize"' in md
    assert 'skill: "memory"' in md
    assert "depends_on: [a]" in md


def test_meta_skill_assemble_rejects_invalid_slots() -> None:
    with pytest.raises(ValueError):
        meta_skill_assemble("p1_sequential", '{"name": "x"}')


def test_meta_skill_fill_slots_with_stub_llm(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    call_log: list[str] = []
    canned_response = json.dumps({
        "name": "synth-pipeline",
        "description": "Synthetic pipeline that does X then Y. Sample for testing fill_slots flow.",
        "meta_priority": 50,
        "triggers": ["synth test"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "process", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save", "with_keys": {}},
        ],
    })

    def stub_llm(prompt: str, **_kwargs) -> str:
        call_log.append(prompt)
        return canned_response

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_llm)

    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(no history)",
        user_intent="process docs then save",
    )
    data = json.loads(result)
    assert data["name"] == "synth-pipeline"
    assert len(call_log) == 1
    # Catalog injection: skill names must appear in prompt
    assert "summarize" in call_log[0]


def test_creator_package_import_registers_tools() -> None:
    """C1 regression: importing the creator package must register creator tools
    in the default ToolRegistry. Phase 1 cross-task review found that the
    @tool decorators only run when the module is imported — production code
    must import opensquilla.skills.creator somewhere in the meta-skill branch."""
    import importlib

    import opensquilla.skills.creator
    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import get_default_registry
    names = get_default_registry().list_names()
    meta_names = sorted(n for n in names if n.startswith("meta"))
    assert "meta_skill_assemble" in names, (
        f"meta_skill_assemble not registered; got: {meta_names}"
    )
    assert "meta_skill_fill_slots" in names, "meta_skill_fill_slots not registered"
    assert "meta_skill_patch_proposal" in names, "meta_skill_patch_proposal not registered"
    assert "meta_skill_refresh_proposal_gates" in names, (
        "meta_skill_refresh_proposal_gates not registered"
    )
    assert "meta_skill_extract_proposal_id" in names, (
        "meta_skill_extract_proposal_id not registered"
    )
    assert "meta_skill_benchmark_proposals" in names, (
        "meta_skill_benchmark_proposals not registered"
    )
    assert "meta_skill_extract_benchmark_proposal_ids" in names, (
        "meta_skill_extract_benchmark_proposal_ids not registered"
    )
    assert "meta_skill_rollback_skill" in names, (
        "meta_skill_rollback_skill not registered"
    )


def test_meta_skill_fill_slots_retries_once_on_validation_error(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    responses = iter([
        '{"name": "bad"}',  # missing fields → ValidationError
        json.dumps({
            "name": "synth-pipeline",
            "description": "Synthetic pipeline that does X then Y. Sample.",
            "meta_priority": 50,
            "triggers": ["synth test"],
            "steps": [
                {"id": "a", "skill": "summarize", "task": "process", "with_keys": {}},
                {"id": "b", "skill": "memory", "task": "save", "with_keys": {}},
            ],
        }),
    ])
    prompts: list[str] = []

    def stub_llm(prompt: str, **_kwargs) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_llm)

    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(no history)",
        user_intent="process docs then save",
    )
    data = json.loads(result)
    assert data["name"] == "synth-pipeline"
    assert len(prompts) == 2
    # Retry prompt must include the ValidationError feedback
    assert "failed schema validation" in prompts[1] or "errors" in prompts[1]


def test_creator_tools_hidden_from_owner_default() -> None:
    """N1: meta_skill_{assemble,fill_slots} must NOT appear in the default
    owner tool catalog. They are internal orchestrator-only tools."""
    import importlib

    import opensquilla.skills.creator  # trigger @tool registration
    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import ToolContext, get_default_registry

    reg = get_default_registry()
    # Use the default owner context (is_owner=True, no allowed_tools override).
    # _iter_visible_tools with this context filters out exposed_by_default=False.
    ctx = ToolContext(is_owner=True)
    visible_names = {rt.spec.name for rt in reg._iter_visible_tools(ctx)}

    for tool_name in (
        "meta_skill_assemble",
        "meta_skill_fill_slots",
        "meta_skill_patch_proposal",
        "meta_skill_extract_proposal_id",
        "meta_skill_benchmark_proposals",
        "meta_skill_extract_benchmark_proposal_ids",
        "meta_skill_rollback_skill",
    ):
        assert tool_name not in visible_names, (
            f"{tool_name} is visible in the default owner tool catalog; "
            "N1 fix requires exposed_by_default=False so C1 lazy import "
            "does not leak it into normal owner turns."
        )

    # But the tools must still be registered (reachable by name for tool_invoker).
    registered_names = set(reg.list_names())
    assert "meta_skill_assemble" in registered_names
    assert "meta_skill_fill_slots" in registered_names
    assert "meta_skill_patch_proposal" in registered_names
    assert "meta_skill_refresh_proposal_gates" in registered_names
    assert "meta_skill_extract_proposal_id" in registered_names
    assert "meta_skill_benchmark_proposals" in registered_names
    assert "meta_skill_extract_benchmark_proposal_ids" in registered_names
    assert "meta_skill_rollback_skill" in registered_names


def test_resolve_provider_config_honors_env_overrides(monkeypatch, tmp_path) -> None:
    """N14: GatewayConfig.load() honours OPENSQUILLA_LLM_* env vars; creator
    must respect the same resolution path."""
    # Point to a non-existent config so the TOML path is empty but env wins.
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENSQUILLA_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "test-key")

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config
    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "openai"
    assert model == "gpt-4o-mini"
    assert api_key == "test-key"


def test_resolve_provider_config_includes_base_url(monkeypatch, tmp_path) -> None:
    """N14: base_url must flow through (vllm/azure require custom endpoint)."""
    toml = tmp_path / "config.toml"
    toml.write_text(
        "[llm]\n"
        'provider = "openai"\n'
        'model = "gpt-4o-mini"\n'
        'api_key = ""\n'
        'base_url = "https://my-vllm.local/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(toml))
    # Ensure no env-LLM override interferes.
    monkeypatch.delenv("OPENSQUILLA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_BASE_URL", raising=False)

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config
    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "openai"
    assert base_url == "https://my-vllm.local/v1"


def test_slot_filler_rejects_yaml_unsafe_strings() -> None:
    """N2: Pydantic validators reject control chars / quotes that would
    break YAML rendering."""
    import pytest as _pytest

    from opensquilla.skills.creator.patterns.schemas import SequentialStep

    # Acceptable
    SequentialStep(id="ok", skill="summarize", task="simple task")

    # Unacceptable: double quote in task
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill="summarize", task='save "summary"')

    # Unacceptable: newline in task
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill="summarize", task="step 1\nstep 2")

    # Unacceptable: backslash in task
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill="summarize", task="path\\to\\file")

    # Unacceptable: double quote in skill name
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill='sum"marize', task="simple task")


def test_fill_slots_retry_no_type_error_on_custom_validator_error(monkeypatch) -> None:
    """N4 regression: Pydantic v2 custom-validator errors put a raw ValueError
    object in ctx.error which is not JSON-serializable. The retry path must use
    default=str so json.dumps(exc.errors()) doesn't TypeError before the retry
    LLM call fires.

    Triggers the N2 validator (double-quote in task) on the first response,
    then returns a clean payload on the second call. Asserts no TypeError is
    raised and the final result is the clean payload.
    """
    import json as _json

    from opensquilla.skills.creator import proposer

    clean_payload = _json.dumps({
        "name": "synth-pipeline",
        "description": "Synthetic pipeline that does X then Y. Sample for N4 regression.",
        "meta_priority": 50,
        "triggers": ["synth test"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "process input", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save result", "with_keys": {}},
        ],
    })
    # First response: task contains a double-quote — triggers the N2
    # custom validator on SequentialStep and raises ValidationError whose
    # exc.errors() contains a raw ValueError in ctx.error.
    bad_payload = _json.dumps({
        "name": "synth-pipeline",
        "description": "Synthetic pipeline. Sample for N4 regression.",
        "meta_priority": 50,
        "triggers": ["synth test"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": 'save "summary"', "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save result", "with_keys": {}},
        ],
    })

    responses = iter([bad_payload, clean_payload])

    def stub_llm(prompt: str, **_kwargs) -> str:
        return next(responses)

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_llm)

    # Must not raise TypeError; must return clean payload
    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(no history)",
        user_intent="process docs then save",
    )
    data = _json.loads(result)
    assert data["name"] == "synth-pipeline", f"unexpected result: {data}"


def test_creator_tools_registered_via_meta_invoke_module_import() -> None:
    """N10: importing the meta_invoke soft-path module (agent.py) must also
    ensure creator tools are registered. The lazy import added at the top of
    _run_meta_invoke_streaming fires whenever the method is entered; here we
    verify the underlying registration by importing opensquilla.skills.creator
    directly (the same effect as the lazy import) and asserting the registry
    reflects the tools — mirrors the C1 hard-takeover test but for the
    soft-path entry in agent.py.
    """
    import importlib

    # Simulate what the N10 lazy import does when _run_meta_invoke_streaming fires.
    import opensquilla.skills.creator  # noqa: F401
    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import get_default_registry

    reg = get_default_registry()
    names = reg.list_names()
    assert "meta_skill_fill_slots" in names, (
        "N10: meta_skill_fill_slots not registered via soft-path import; "
        f"registered names starting with 'meta': "
        f"{sorted(n for n in names if n.startswith('meta'))}"
    )
    assert "meta_skill_assemble" in names, (
        "N10: meta_skill_assemble not registered via soft-path import"
    )
    assert "meta_skill_patch_proposal" in names, (
        "N10: meta_skill_patch_proposal not registered via soft-path import"
    )
    assert "meta_skill_refresh_proposal_gates" in names, (
        "N10: meta_skill_refresh_proposal_gates not registered via soft-path import"
    )
    assert "meta_skill_extract_proposal_id" in names, (
        "N10: meta_skill_extract_proposal_id not registered via soft-path import"
    )
    assert "meta_skill_benchmark_proposals" in names, (
        "N10: meta_skill_benchmark_proposals not registered via soft-path import"
    )
    assert "meta_skill_extract_benchmark_proposal_ids" in names, (
        "N10: meta_skill_extract_benchmark_proposal_ids not registered via soft-path import"
    )
    assert "meta_skill_rollback_skill" in names, (
        "N10: meta_skill_rollback_skill not registered via soft-path import"
    )


def test_strip_code_fences_handles_json_lang_tag() -> None:
    from opensquilla.skills.creator.proposer import _strip_code_fences

    assert _strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('```JSON\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('{"a": 1}') == '{"a": 1}'
    # Whitespace tolerance
    assert _strip_code_fences('  ```json\n{"a": 1}\n```  ') == '{"a": 1}'


def test_fill_slots_strips_code_fence_before_parsing(monkeypatch) -> None:
    """Fix #A: code-fence-wrapped JSON should parse successfully."""
    from opensquilla.skills.creator import proposer

    canned = json.dumps({
        "name": "fenced-test",
        "description": "test description that meets min length requirement for schema",
        "meta_priority": 50,
        "triggers": ["fenced trigger"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "process", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save", "with_keys": {}},
        ],
    })
    fenced = f"```json\n{canned}\n```"
    monkeypatch.setattr(proposer, "_call_llm_for_slots", lambda prompt, **_: fenced)

    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test intent",
    )
    parsed = json.loads(result)
    assert parsed["name"] == "fenced-test"


def test_fill_slots_validation_error_surfaces_detail(monkeypatch) -> None:
    """Fix #B: ValidationError after retry should include actionable detail
    (response preview + error message), not just generic 'internal error'."""
    from opensquilla.skills.creator import proposer

    monkeypatch.setattr(
        proposer, "_call_llm_for_slots",
        lambda prompt, **_: '{"name": "missing-fields-test"}',  # always invalid
    )

    with pytest.raises((ValueError, Exception)) as exc_info:
        proposer.meta_skill_fill_slots(
            pattern_id="p1_sequential",
            history_summary="(test)",
            user_intent="test",
        )
    err_str = str(exc_info.value)
    # The error message must include the pattern_id and a hint of what failed
    assert "p1_sequential" in err_str or "missing-fields-test" in err_str


def test_fill_slots_prompt_includes_schema_and_example(monkeypatch) -> None:
    """Fix #A: the prompt must include the JSON schema + a concrete example
    so the LLM cannot hallucinate field names like `execution_sequence`.

    Specifically asserts:
    - `triggers` (correct field name) appears in the prompt
    - `steps` (correct field name) appears in the prompt
    - `execution_sequence` appears as an anti-pattern warning (DO NOT use)
    - an example anchors the output (example-pipeline or pdf-toolkit)
    """
    from opensquilla.skills.creator import proposer

    captured: list[str] = []
    canned_resp = json.dumps({
        "name": "ok-pipeline",
        "description": "x" * 50,
        "meta_priority": 50,
        "triggers": ["t"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "t", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "t", "with_keys": {}},
        ],
    })

    def stub_with_capture(prompt: str, **_) -> str:
        captured.append(prompt)
        return canned_resp

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_with_capture)
    proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test",
    )

    assert captured, "stub was never called"
    prompt = captured[0]
    # Schema field names must appear in the prompt
    assert "triggers" in prompt, "prompt missing 'triggers' field name"
    assert '"steps"' in prompt or "'steps'" in prompt or "steps" in prompt, (
        "prompt missing 'steps' field name"
    )
    # Anti-pattern warning must name the wrong field so LLM is explicitly told not to use it
    assert "execution_sequence" in prompt, (
        "prompt must warn against 'execution_sequence' so LLM does not invent it"
    )
    # Example must anchor the output with concrete field values
    assert "example-pipeline" in prompt or "pdf-toolkit" in prompt, (
        "prompt missing example anchor (example-pipeline or pdf-toolkit)"
    )


def test_resolve_provider_config_accepts_empty_api_key(tmp_path, monkeypatch) -> None:
    """N11: _resolve_provider_from_config must return a valid triple when
    provider and model are set but api_key is absent (keyless local providers
    such as ollama / lm_studio). Previously the `and api_key` truthy guard
    returned (None, None, None), causing the resolution to fall through to
    env-var scan and ultimately raise RuntimeError on keyless deployments."""
    config_toml = tmp_path / "opensquilla.toml"
    config_toml.write_text(
        '[llm]\nprovider = "ollama"\nmodel = "llama3"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_toml))

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config

    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "ollama", (
        f"N11: expected 'ollama', got {provider!r}; "
        "keyless provider must not be rejected by _resolve_provider_from_config"
    )
    assert model == "llama3"
    assert api_key == ""  # empty string is correct for ollama


def test_resolve_provider_config_env_override_beats_toml(tmp_path, monkeypatch) -> None:
    """Fix #C: OPENSQUILLA_LLM_MODEL env var must win over a TOML [llm] section.

    When a config.toml has [llm] provider/model values, pydantic-settings'
    nested env binding is bypassed (the parent passes the TOML dict directly).
    _resolve_provider_from_config must apply a post-override so the env vars
    always beat TOML content.
    """
    config_toml = tmp_path / "opensquilla.toml"
    config_toml.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "deepseek/deepseek-v3.1-terminus"\n'
        'api_key = "toml-key"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_toml))
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSQUILLA_LLM_MODEL", "claude-3-5-haiku-20241022")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "env-key")

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config

    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "anthropic", (
        f"Fix #C: env var OPENSQUILLA_LLM_PROVIDER must beat TOML; got {provider!r}"
    )
    assert model == "claude-3-5-haiku-20241022", (
        f"Fix #C: env var OPENSQUILLA_LLM_MODEL must beat TOML; got {model!r}"
    )
    assert api_key == "env-key", (
        f"Fix #C: env var OPENSQUILLA_LLM_API_KEY must beat TOML; got {api_key!r}"
    )


@pytest.mark.asyncio
async def test_fill_slots_tool_validation_error_returns_structured_json(monkeypatch) -> None:
    """Fix #B (Option B1): when fill_slots raises _FillSlotsValidationError after
    exhausting all retries, the @tool wrapper must catch it and return a structured
    JSON error dict rather than letting it propagate as a generic 'internal error'
    through the envelope layer."""
    from opensquilla.skills.creator import proposer

    # Always return invalid JSON to exhaust retries
    monkeypatch.setattr(
        proposer, "_call_llm_for_slots",
        lambda prompt, **_: '{"name": "x"}',  # missing required fields
    )

    result = await proposer.meta_skill_fill_slots_tool(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test",
    )
    # The tool must not raise; it returns JSON with _creator_error key
    payload = json.loads(result)
    assert payload.get("_creator_error") == "validation_failed_after_retry", (
        f"Fix #B: expected _creator_error='validation_failed_after_retry', got: {payload}"
    )
    assert "p1_sequential" in payload.get("pattern_id", ""), (
        f"Fix #B: pattern_id missing from error payload: {payload}"
    )
    assert "detail" in payload, f"Fix #B: 'detail' key missing from error payload: {payload}"


def test_sequential_slots_accept_generation_rationale() -> None:
    slots = SequentialSlots(
        name="test-rationale",
        description="Synthetic pipeline with explicit generation rationale.",
        triggers=["rationale trigger"],
        steps=[
            {"id": "a", "skill": "summarize", "task": "process input"},
            {"id": "b", "skill": "memory", "task": "save result"},
        ],
        generation_rationale={
            "intent": "Turn source material into a saved summary.",
            "target_outcome": "The user gets a concise summary and durable memory entry.",
            "stop_condition": "Summary saved and final response reports completion evidence.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["user asked for process then save"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": [
                "ordinary_skill: requires two existing skills in sequence",
            ],
            "output_contract_summary": "Final answer reports summary and save status.",
        },
    )

    assert slots.generation_rationale.selected_shape == "metaskill"
    assert slots.generation_rationale.selected_pattern == "p1_sequential"


def test_fill_slots_prompt_requires_generation_rationale(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    captured: list[str] = []
    canned_resp = json.dumps({
        "name": "ok-pipeline",
        "description": "x" * 50,
        "meta_priority": 50,
        "triggers": ["t"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "t", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "t", "with_keys": {}},
        ],
        "generation_rationale": {
            "intent": "Create a concise two-step workflow.",
            "target_outcome": "The user gets processed text and saved output.",
            "stop_condition": "The final step reports saved output.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["user requested two-step workflow"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": ["bundle: steps depend on prior output"],
            "output_contract_summary": "Final answer reports processing and save status.",
        },
    })

    def stub_with_capture(prompt: str, **_) -> str:
        captured.append(prompt)
        return canned_resp

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_with_capture)
    proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test",
    )

    assert captured
    prompt = captured[0]
    assert "generation_rationale" in prompt
    assert "selected_shape" in prompt
    assert "rejected_alternatives" in prompt
    assert "Do not invent tools, gates, inputs, or output contracts" in prompt


def test_creator_quality_and_activation_tools_registered() -> None:
    import importlib

    import opensquilla.skills.creator

    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import get_default_registry

    reg = get_default_registry()
    names = set(reg.list_names())
    assert "meta_skill_generation_quality_run" in names
    assert "meta_skill_activation_eval_run" in names
    assert "meta_skill_benchmark_proposals" in names
    assert "meta_skill_rollback_skill" in names

    activation_tool = reg.get("meta_skill_activation_eval_run")
    assert activation_tool is not None
    assert activation_tool.spec.exposed_by_default is False
    assert "catalog_negative_prompts" in activation_tool.spec.parameters
    assert "threshold" not in activation_tool.spec.parameters
    benchmark_tool = reg.get("meta_skill_benchmark_proposals")
    assert benchmark_tool is not None
    assert benchmark_tool.spec.exposed_by_default is False
    assert "target_json" in benchmark_tool.spec.parameters
    rollback_tool = reg.get("meta_skill_rollback_skill")
    assert rollback_tool is not None
    assert rollback_tool.spec.exposed_by_default is False
    assert "skill_name" in rollback_tool.spec.parameters


def test_generation_quality_tool_returns_utf8_json_string() -> None:
    from opensquilla.skills.creator import proposer

    slots = {
        "name": "unicode-quality",
        "description": "Synthetic pipeline that preserves non-ASCII trigger text.",
        "meta_priority": 50,
        "triggers": ["摘要流程"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "process input"},
            {"id": "b", "skill": "memory", "task": "save result"},
        ],
        "generation_rationale": {
            "intent": "Turn source material into a saved summary.",
            "target_outcome": "The user gets a concise summary and durable memory entry.",
            "stop_condition": "Summary saved and final response reports completion evidence.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["user asked for process then save"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": ["需要人工确认输入范围"],
            "rejected_alternatives": [
                "ordinary_skill: requires two existing skills in sequence",
            ],
            "output_contract_summary": "Final answer reports summary and save status.",
        },
    }

    result = proposer.meta_skill_generation_quality_run(
        "p1_sequential",
        json.dumps(slots, ensure_ascii=False),
    )

    assert isinstance(result, str)
    assert "需要人工确认输入范围" in result
    payload = json.loads(result)
    assert payload["passed"] is True


def test_activation_eval_tool_uses_catalog_negative_prompts(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    skill_md = """---
name: synth-alpha-report
description: "Synthetic alpha report workflow."
kind: meta
meta_priority: 50
triggers:
  - "alpha report"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""
    captured: dict[str, object] = {}

    def stub_evaluator(
        skill_markdown,
        positive_prompts,
        negative_prompts,
        *,
        threshold,
    ):
        captured["skill_markdown"] = skill_markdown
        captured["positive_prompts"] = positive_prompts
        captured["negative_prompts"] = negative_prompts
        captured["threshold"] = threshold
        return {
            "required": True,
            "passed": True,
            "reason": "ok",
            "true_positive_rate": 1.0,
            "false_positive_count": 0,
            "cases": [],
            "issues": [],
            "metadata": {},
        }

    monkeypatch.setattr(proposer, "evaluate_candidate_activation", stub_evaluator)

    result = proposer.meta_skill_activation_eval_run(
        skill_md=skill_md,
        positive_prompts=json.dumps(["please run the alpha report"]),
        catalog_negative_prompts="please run the beta digest\nplease summarize this note",
        threshold=0.8,
    )

    payload = json.loads(result)
    assert payload["passed"] is True
    assert captured == {
        "skill_markdown": skill_md,
        "positive_prompts": ["please run the alpha report"],
        "negative_prompts": [
            "please run the beta digest",
            "please summarize this note",
        ],
        "threshold": 0.8,
    }


@pytest.mark.asyncio
async def test_activation_eval_tool_wrapper_accepts_catalog_negative_prompts(
    monkeypatch,
) -> None:
    import asyncio

    from opensquilla.skills.creator import proposer

    skill_md = """---
name: synth-alpha-report
description: "Synthetic alpha report workflow."
kind: meta
meta_priority: 50
triggers:
  - "alpha report"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""
    captured: dict[str, object] = {}

    async def fake_to_thread(func, *args, **kwargs):
        captured["func"] = func
        captured["args"] = args
        captured["kwargs"] = kwargs
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = await proposer.meta_skill_activation_eval_run_tool(
        skill_md=skill_md,
        positive_prompts=json.dumps(["please run the alpha report"]),
        catalog_negative_prompts=json.dumps([
            "please run the beta digest",
            "please summarize this note",
        ]),
    )

    payload = json.loads(result)
    assert payload["passed"] is True
    assert captured["func"] is proposer.meta_skill_activation_eval_run
    assert captured["args"] == (
        skill_md,
        json.dumps(["please run the alpha report"]),
        "",
        json.dumps([
            "please run the beta digest",
            "please summarize this note",
        ]),
    )
    assert captured["kwargs"] == {}
    assert [case["kind"] for case in payload["cases"]] == [
        "positive",
        "negative",
        "negative",
    ]


def test_activation_eval_tool_empty_prompt_strings_flow_to_structural_failure() -> None:
    from opensquilla.skills.creator import proposer

    skill_md = """---
name: synth-alpha-report
description: "Synthetic alpha report workflow."
triggers:
  - "alpha report"
---
"""

    result = proposer.meta_skill_activation_eval_run(skill_md=skill_md)

    payload = json.loads(result)
    assert payload["passed"] is False
    assert "missing_positive_prompts" in payload["issues"]
    assert "missing_negative_prompts" in payload["issues"]


def test_activation_eval_tool_auto_prompts_derive_passing_fixtures() -> None:
    from opensquilla.skills.creator import proposer

    skill_md = """---
name: synth-alpha-report
description: "Synthetic alpha report workflow."
kind: meta
meta_priority: 50
triggers:
  - "alpha report"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""

    result = proposer.meta_skill_activation_eval_run(
        skill_md=skill_md,
        positive_prompts="auto",
        catalog_negative_prompts="auto",
    )

    payload = json.loads(result)
    assert payload["passed"] is True
    assert payload["true_positive_rate"] == 1.0
    assert payload["false_positive_count"] == 0
    assert [case["kind"] for case in payload["cases"]] == [
        "positive",
        "negative",
        "negative",
        "negative",
        "negative",
    ]
    assert payload["cases"][0]["prompt"] == "please use alpha report"


def test_persist_proposal_forwards_generation_quality_and_activation_results(monkeypatch) -> None:
    from types import SimpleNamespace

    from opensquilla.skills.creator import proposer

    captured: dict[str, object] = {}

    def fake_run(args, *, capture_output, text, check):
        captured["args"] = args
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["check"] = check
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "ok", "proposal_id": "proposal-1"}),
            stderr="",
        )

    monkeypatch.setattr(proposer.subprocess, "run", fake_run)

    out = json.loads(proposer.meta_skill_persist_proposal(
        skill_md="---\nname: synth-test\n---\n",
        lint_result='{"G1": {"passed": true}}',
        smoke_result='{"G3": {"passed": true}}',
        generation_quality_result='{"passed": true}',
        activation_result='{"passed": true}',
    ))

    assert out["proposal_id"] == "proposal-1"
    args = captured["args"]
    assert isinstance(args, list)
    assert args[args.index("--generation-quality-result") + 1] == '{"passed": true}'
    assert args[args.index("--activation-result") + 1] == '{"passed": true}'


def test_persist_proposal_forwards_bundle_files_json(monkeypatch) -> None:
    from types import SimpleNamespace

    from opensquilla.skills.creator import proposer

    captured: dict[str, object] = {}

    def fake_run(args, *, capture_output, text, check):
        captured["args"] = args
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "ok", "proposal_id": "proposal-1"}),
            stderr="",
        )

    monkeypatch.setattr(proposer.subprocess, "run", fake_run)

    out = json.loads(proposer.meta_skill_persist_proposal(
        skill_md="---\nname: synth-test\n---\n",
        lint_result='{"G1": {"passed": true}}',
        smoke_result='{"G3": {"passed": true}}',
        bundle_files_json='{"scripts/render.py": "print(1)\\n"}',
    ))

    assert out["proposal_id"] == "proposal-1"
    args = captured["args"]
    assert isinstance(args, list)
    assert args[args.index("--bundle-files-json") + 1] == (
        '{"scripts/render.py": "print(1)\\n"}'
    )


def test_patch_proposal_tool_wrapper_creates_child_revision(tmp_path) -> None:
    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    home = tmp_path / ".opensquilla"
    skill_md = """---
name: synth-wrapper-patch
description: "Wrapper patch test meta-skill."
kind: meta
meta_priority: 50
triggers:
  - "wrapper patch"
composition:
  steps:
    - id: digest
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""
    parent = proposals_lib.write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
        creator_mode="PERSISTED_PROPOSAL",
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )
    patch_json = json.dumps({
        "add_triggers": ["wrapper patch revised"],
        "owner": "test-operator",
    })

    out = json.loads(proposer.meta_skill_patch_proposal(
        parent["proposal_id"],
        patch_json,
        home=str(home),
    ))

    assert out["status"] == "ok"
    assert out["parent_proposal_id"] == parent["proposal_id"]
    assert out["proposal_id"] != parent["proposal_id"]
    child_dir = home / "proposals" / out["proposal_id"]
    assert "wrapper patch revised" in (child_dir / "SKILL.md").read_text(encoding="utf-8")
    gates = json.loads((child_dir / "gates.json").read_text(encoding="utf-8"))
    assert gates["creator_mode"] == "PATCH_PROPOSAL"
    assert gates["revision"]["owner"] == "test-operator"


def test_refresh_proposal_tool_wrapper_updates_revision_gates(tmp_path) -> None:
    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    home = tmp_path / ".opensquilla"
    skill_md = """---
name: synth-wrapper-refresh
description: "Wrapper refresh test meta-skill."
kind: meta
meta_priority: 50
triggers:
  - "wrapper refresh"
composition:
  steps:
    - id: summarize
      skill: summarize
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""
    parent = proposals_lib.write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    patched = proposals_lib.patch_proposal(
        home,
        parent["proposal_id"],
        {"add_triggers": ["wrapper refreshed trigger"]},
    )

    out = json.loads(proposer.meta_skill_refresh_proposal_gates(
        proposal_id=patched["proposal_id"],
        smoke_json=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
        collision_result="PASS: wrapper refreshed collision",
        risk_result="RISK: low\nCAPABILITIES:\n- read-only",
        generation_quality_json=json.dumps({"passed": True}),
        activation_json=json.dumps({"passed": True}),
        home=str(home),
    ))

    assert out["status"] == "ok"
    assert "collision_check" in out["refreshed"]
    gates = json.loads(
        (home / "proposals" / patched["proposal_id"] / "gates.json").read_text(),
    )
    assert gates["collision_check"]["passed"] is True


def test_extract_proposal_id_prefers_explicit_fallback() -> None:
    from opensquilla.skills.creator import proposer

    assert proposer.meta_skill_extract_proposal_id(
        "revise proposal abcd1234",
        fallback="deadbeef",
    ) == "deadbeef"
    assert proposer.meta_skill_extract_proposal_id("revise proposal abcd1234") == "abcd1234"
    assert proposer.meta_skill_extract_proposal_id("revise proposal ABCD1234") == ""


def test_patch_proposal_tool_wrapper_uses_default_state_home(
    tmp_path,
    monkeypatch,
) -> None:
    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    home = tmp_path / "state-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    skill_md = """---
name: synth-env-home-patch
description: "Patch wrapper honors the configured OpenSquilla state home."
kind: meta
meta_priority: 50
triggers:
  - "env home patch"
composition:
  steps:
    - id: digest
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""
    parent = proposals_lib.write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
        creator_mode="PERSISTED_PROPOSAL",
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )

    out = json.loads(proposer.meta_skill_patch_proposal(
        parent["proposal_id"],
        '{"append_body": "Revision note."}',
    ))

    assert out["status"] == "ok"
    child_dir = home / "proposals" / out["proposal_id"]
    assert child_dir.is_dir()
    assert "Revision note." in (child_dir / "SKILL.md").read_text(encoding="utf-8")


def test_benchmark_proposals_tool_wrapper_records_report(tmp_path) -> None:
    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    home = tmp_path / ".opensquilla"
    skill_md = """---
name: synth-wrapper-benchmark
description: "Wrapper benchmark test meta-skill."
kind: meta
meta_priority: 50
triggers:
  - "wrapper benchmark"
composition:
  steps:
    - id: digest
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""
    baseline = proposals_lib.write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
        creator_mode="PERSISTED_PROPOSAL",
        generation_quality_result={"passed": True},
        activation_result={"passed": True},
    )
    candidate = proposals_lib.patch_proposal(
        home,
        baseline["proposal_id"],
        {
            "append_eval_prompts": [{
                "name": "wrapper-benchmark",
                "prompt": "please use wrapper benchmark",
                "rubric": ["Summary"],
            }],
        },
    )

    out = json.loads(proposer.meta_skill_benchmark_proposals(
        target_json=json.dumps({
            "baseline_proposal_id": baseline["proposal_id"],
            "candidate_proposal_id": candidate["proposal_id"],
        }),
        comparison_json=json.dumps({
            "passed": True,
            "winner": "candidate",
            "cases": [{
                "prompt": "please use wrapper benchmark",
                "winner": "candidate",
                "regression": "",
            }],
        }),
        home=str(home),
    ))

    assert out["status"] == "ok"
    report = json.loads(
        (
            home
            / "proposal-benchmarks"
            / out["benchmark_id"]
            / "benchmark.json"
        ).read_text(encoding="utf-8"),
    )
    assert report["creator_mode"] == "BENCHMARK"
    assert report["gates"]["benchmark_compare"]["passed"] is True


def test_extract_benchmark_proposal_ids_uses_fallbacks_then_text() -> None:
    from opensquilla.skills.creator import proposer

    assert json.loads(proposer.meta_skill_extract_benchmark_proposal_ids(
        "benchmark abcd1234 against deadbeef",
        baseline_fallback="face1234",
        candidate_fallback="cafe1234",
    )) == {
        "baseline_proposal_id": "face1234",
        "candidate_proposal_id": "cafe1234",
    }
    assert json.loads(proposer.meta_skill_extract_benchmark_proposal_ids(
        "benchmark abcd1234 against deadbeef",
    )) == {
        "baseline_proposal_id": "abcd1234",
        "candidate_proposal_id": "deadbeef",
    }


def test_rollback_skill_tool_wrapper_restores_previous_version(tmp_path) -> None:
    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    home = tmp_path / ".opensquilla"
    skill_md = """---
name: synth-wrapper-rollback
description: "Original rollback wrapper skill."
kind: meta
meta_priority: 50
triggers:
  - "wrapper rollback"
composition:
  steps:
    - id: digest
      skill: summarize
      with:
        text: "{{ inputs.user_message }}"
---
"""
    original = proposals_lib.write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    assert proposals_lib.accept_proposal(home, original["proposal_id"])["status"] == "ok"
    replacement = proposals_lib.write_proposal(
        home,
        skill_md.replace("Original rollback", "Replacement rollback"),
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )
    assert proposals_lib.accept_proposal(
        home,
        replacement["proposal_id"],
        replace=True,
    )["status"] == "ok"

    out = json.loads(proposer.meta_skill_rollback_skill(
        "synth-wrapper-rollback",
        home=str(home),
    ))

    assert out["status"] == "ok"
    assert out["restored_proposal_id"] == original["proposal_id"]
    assert "Original rollback wrapper skill" in (
        home / "skills" / "synth-wrapper-rollback" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_fill_slots_prompt_includes_draft_seed(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    captured: list[str] = []
    canned_resp = json.dumps({
        "name": "vendor-brief-pipeline",
        "description": "Research a vendor and produce a concise decision brief.",
        "meta_priority": 50,
        "triggers": ["vendor decision brief"],
        "steps": [
            {
                "id": "research",
                "skill": "summarize",
                "task": "Research the vendor.",
                "with_keys": {},
            },
            {
                "id": "draft",
                "skill": "summarize",
                "task": "Draft the brief.",
                "with_keys": {},
            },
        ],
        "generation_rationale": {
            "intent": "Reuse a successful vendor brief workflow.",
            "target_outcome": "The user gets a decision-ready vendor brief.",
            "stop_condition": "The brief contains recommendation and evidence.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["draft seed observed summarize steps"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": ["bundle: observed steps depend on prior output"],
            "output_contract_summary": "Final answer includes recommendation and evidence.",
        },
    })

    def stub_with_capture(prompt: str, **_) -> str:
        captured.append(prompt)
        return canned_resp

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_with_capture)
    proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="",
        user_intent="Create from run.",
        draft_seed_json=json.dumps({
            "goal": "Research a vendor and produce a decision brief.",
            "observed_steps": ["summarize", "summarize"],
            "negative_cases": ["Do not use for generic vendor facts."],
            "constraints": ["Keep trigger narrow."],
            "evidence_refs": ["run_01"],
        }),
    )

    prompt = captured[0]
    assert "## Draft seed" in prompt
    assert "Research a vendor and produce a decision brief" in prompt
    assert "Do not use for generic vendor facts" in prompt
    assert "Treat draft_seed_json as evidence, not as permission to bypass gates" in prompt


def test_fill_slots_prompt_preserves_later_draft_seed_keys_when_early_field_is_large(
    monkeypatch,
) -> None:
    from opensquilla.skills.creator import proposer

    captured: list[str] = []
    canned_resp = json.dumps({
        "name": "vendor-brief-pipeline",
        "description": "Research a vendor and produce a concise decision brief.",
        "meta_priority": 50,
        "triggers": ["vendor decision brief"],
        "steps": [
            {"id": "research", "skill": "summarize", "task": "Research.", "with_keys": {}},
            {"id": "draft", "skill": "summarize", "task": "Draft.", "with_keys": {}},
        ],
        "generation_rationale": {
            "intent": "Reuse a successful vendor brief workflow.",
            "target_outcome": "The user gets a decision-ready vendor brief.",
            "stop_condition": "The brief contains recommendation and evidence.",
            "selected_shape": "metaskill",
            "selected_pattern": "p1_sequential",
            "source_evidence": ["draft seed observed summarize steps"],
            "filled_slots": ["name", "description", "triggers", "steps"],
            "unresolved_assumptions": [],
            "rejected_alternatives": ["bundle: observed steps depend on prior output"],
            "output_contract_summary": "Final answer includes recommendation and evidence.",
        },
    })

    def stub_with_capture(prompt: str, **_) -> str:
        captured.append(prompt)
        return canned_resp

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_with_capture)
    proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="",
        user_intent="Create from run.",
        draft_seed_json=json.dumps({
            "goal": "Research a vendor. " + ("oversized early detail " * 400),
            "observed_steps": ["summarize", "summarize"],
            "negative_cases": ["NEGATIVE_CASE_MARKER do not trigger on generic facts."],
            "duplicate_detection": {
                "status": "DUPLICATE_DETECTION_MARKER possible_overlap",
            },
        }),
    )

    prompt = captured[0]
    assert "NEGATIVE_CASE_MARKER" in prompt
    assert "DUPLICATE_DETECTION_MARKER" in prompt


def test_fill_slots_tool_schema_accepts_draft_seed_json() -> None:
    from opensquilla.skills.creator.proposer import meta_skill_fill_slots_tool

    schema = meta_skill_fill_slots_tool.tool.input_schema
    assert "draft_seed_json" in schema["properties"]
    assert "draft_seed_json" not in schema["required"]
