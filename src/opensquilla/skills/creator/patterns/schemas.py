"""Pydantic slot schemas for meta-skill-creator patterns."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

_YAML_UNSAFE_CHARS = ('"', "\n", "\r", "\\")


def _check_yaml_safe(v: str, field_name: str) -> str:
    """Reject characters that would produce invalid YAML when interpolated."""
    for ch in _YAML_UNSAFE_CHARS:
        if ch in v:
            raise ValueError(
                f"{field_name!r} may not contain double quotes, newlines, or "
                "backslashes (would break YAML rendering in Jinja templates)"
            )
    return v


class GenerationRationale(BaseModel):
    intent: str = Field(min_length=10, max_length=400)
    target_outcome: str = Field(min_length=10, max_length=400)
    stop_condition: str = Field(min_length=10, max_length=400)
    selected_shape: str = Field(pattern=r"^(ordinary_skill|metaskill|bundle|patch|refusal)$")
    selected_pattern: str = Field(min_length=2, max_length=80)
    source_evidence: list[str] = Field(min_length=1, max_length=8)
    filled_slots: list[str] = Field(min_length=1, max_length=16)
    unresolved_assumptions: list[str] = Field(default_factory=list, max_length=8)
    rejected_alternatives: list[str] = Field(min_length=1, max_length=8)
    output_contract_summary: str = Field(min_length=10, max_length=500)

    @field_validator(
        "intent",
        "target_outcome",
        "stop_condition",
        "selected_shape",
        "selected_pattern",
        "output_contract_summary",
    )
    @classmethod
    def _scalar_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "generation_rationale scalar")

    @field_validator(
        "source_evidence",
        "filled_slots",
        "unresolved_assumptions",
        "rejected_alternatives",
        mode="before",
    )
    @classmethod
    def _list_items_yaml_safe(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    _check_yaml_safe(item, "generation_rationale item")
        return v


class SequentialStep(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    skill: str
    task: str = Field(max_length=400)
    with_keys: dict[str, str] = Field(default_factory=dict)

    @field_validator("task")
    @classmethod
    def _task_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "task")

    @field_validator("skill")
    @classmethod
    def _skill_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "skill")


class SequentialSlots(BaseModel):
    name: str = Field(
        min_length=3, max_length=64,
        pattern=r"^[a-z][a-z0-9_\-]{2,63}$",
        description="Skill name: lowercase alpha-num-hyphen-underscore, 3-64 chars",
    )
    description: str = Field(min_length=30, max_length=200)
    meta_priority: int = Field(ge=30, le=80, default=50)
    triggers: list[str] = Field(min_length=1, max_length=8)
    steps: list[SequentialStep] = Field(min_length=2, max_length=5)
    generation_rationale: GenerationRationale | None = None

    @field_validator("description")
    @classmethod
    def _description_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "description")

    @field_validator("triggers", mode="before")
    @classmethod
    def _triggers_yaml_safe(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    _check_yaml_safe(item, "triggers item")
        return v


class FanOutBranch(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    skill: str
    task: str = Field(max_length=400)
    with_keys: dict[str, str] = Field(default_factory=dict)

    @field_validator("task")
    @classmethod
    def _task_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "task")

    @field_validator("skill")
    @classmethod
    def _skill_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "skill")


class FanOutTail(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    skill: str
    task: str = Field(max_length=400)
    with_keys: dict[str, str] = Field(default_factory=dict)

    @field_validator("task")
    @classmethod
    def _task_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "task")

    @field_validator("skill")
    @classmethod
    def _skill_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "skill")


class FanOutMergeSlots(BaseModel):
    name: str = Field(
        min_length=3, max_length=64,
        pattern=r"^[a-z][a-z0-9_\-]{2,63}$",
        description="Skill name: lowercase alpha-num-hyphen-underscore, 3-64 chars",
    )
    description: str = Field(min_length=30, max_length=200)
    meta_priority: int = Field(ge=30, le=80, default=50)
    triggers: list[str] = Field(min_length=1, max_length=8)
    branches: list[FanOutBranch] = Field(min_length=2, max_length=4)
    merge: FanOutBranch
    tail: FanOutTail | None = None
    generation_rationale: GenerationRationale | None = None

    @field_validator("description")
    @classmethod
    def _description_yaml_safe(cls, v: str) -> str:
        return _check_yaml_safe(v, "description")

    @field_validator("triggers", mode="before")
    @classmethod
    def _triggers_yaml_safe(cls, v: object) -> object:
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    _check_yaml_safe(item, "triggers item")
        return v
