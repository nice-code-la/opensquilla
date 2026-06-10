from __future__ import annotations

from pathlib import Path

from opensquilla.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SKILLS = ROOT / "src" / "opensquilla" / "skills" / "bundled"
MEMORY_SKILL = BUNDLED_SKILLS / "memory" / "SKILL.md"


def test_memory_skill_is_parseable_and_gated_on_read_tools(tmp_path: Path) -> None:
    loader = SkillLoader(
        bundled_dir=BUNDLED_SKILLS,
        snapshot_path=tmp_path / "skills_snapshot.json",
    )

    skill = next(s for s in loader.load_all() if s.name == "memory")

    assert skill.description.startswith("Use when")
    assert skill.requires_tools == ["memory_search", "memory_get"]
    assert skill.disable_model_invocation is False
    assert skill.provenance.origin == "opensquilla-original"
    assert skill.provenance.maintained_by == "OpenSquilla"


def test_loader_maps_creator_visibility_aliases_to_runtime_fields(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "bundled" / "alias-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: alias-skill
description: "Uses creator visibility aliases"
metadata:
  opensquilla:
    requires_toolsets:
      - browser
      - filesystem
    fallback_for_tools:
      - plain_shell
---
Alias skill body.
""",
        encoding="utf-8",
    )
    loader = SkillLoader(
        bundled_dir=tmp_path / "bundled",
        snapshot_path=tmp_path / "skills_snapshot.json",
    )

    skill = next(s for s in loader.load_all() if s.name == "alias-skill")

    assert skill.requires_tools == ["browser", "filesystem"]
    assert skill.fallback_for_toolsets == ["plain_shell"]


def test_memory_skill_documents_usable_write_and_forget_paths() -> None:
    text = MEMORY_SKILL.read_text(encoding="utf-8")
    lower = text.lower()

    assert "origin: opensquilla-original" in text
    assert "Use only tools that are visible in the current tool list" in text
    assert "memory_save" in text
    assert "if `memory_save` is available" in lower
    assert "`MEMORY.md`" in text and "mode='replace'" in text
    assert "memory/**/*.md" in text
    assert "memory_delete" in text
    assert "If no write or delete tool is available" in text
    assert "Only confirm memory was updated after the write or delete succeeds" in text
