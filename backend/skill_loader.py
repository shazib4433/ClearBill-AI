"""Stage 4: Dynamic Payer Skill Loader reading .agents/skills/<payer>-audit/SKILL.md."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.config import get_settings

PAYER_TO_SKILL_SLUG: dict[str, str] = {
    "ECHS": "echs-tariff-audit",
    "CGHS": "cghs-tariff-audit",
    "TPA": "tpa-cashless-audit",
    "PM-JAY": "pmjay-package-audit",
    "PMJAY": "pmjay-package-audit",
}


@dataclass(frozen=True)
class LoadedPayerSkill:
    slug: str
    name: str
    description: str
    path: str
    content: str


def _parse_frontmatter(raw_md: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw_md, flags=re.DOTALL)
    if not match:
        return {}, raw_md
    fm_block, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    current_key: str | None = None
    buffer: list[str] = []
    for line in fm_block.splitlines():
        if re.match(r"^[A-Za-z0-9_-]+\s*:", line):
            if current_key is not None:
                meta[current_key] = " ".join(buffer).strip().strip('"').strip("'")
            key, val = line.split(":", 1)
            current_key = key.strip()
            val = val.strip()
            buffer = [] if val in (">-", "|", ">", "|-") else [val]
        elif current_key is not None:
            buffer.append(line.strip())
    if current_key is not None:
        meta[current_key] = " ".join(buffer).strip().strip('"').strip("'")
    return meta, body.strip()


def load_payer_skill(payer_category: str) -> LoadedPayerSkill:
    """Resolve and load the matching SKILL.md rulebook for the patient's payer category."""
    normalized = (payer_category or "").strip().upper()
    slug = PAYER_TO_SKILL_SLUG.get(normalized)

    if slug is None:
        # Self-Pay or general fallback rulebook
        return LoadedPayerSkill(
            slug="self-pay-fairness-audit",
            name="self-pay-fairness-audit",
            description="Standard clinical billing integrity audit for Self-Pay discharges.",
            path="builtin://self-pay-fairness-audit",
            content=(
                "# Self-Pay Discharge Billing Integrity Rules\n\n"
                "- Flag duplicate shift charges posted without repeat clinical orders (`FLAGGED_DUPLICATE`).\n"
                "- Flag high-cost diagnostics or ICU days lacking clinical notes or doctor orders (`FLAGGED_NO_MEDICAL_NECESSITY`).\n"
                "- Preserve every medically justified and ordered charge as `VERIFIED_CLEAN`."
            ),
        )

    settings = get_settings()
    skill_file = settings.skills_dir / slug / "SKILL.md"
    if not skill_file.exists():
        raise FileNotFoundError(f"Required payer skill rulebook missing: {skill_file}")

    raw = skill_file.read_text(encoding="utf-8")
    meta, _body = _parse_frontmatter(raw)
    rel_path = f".agents/skills/{slug}/SKILL.md"
    return LoadedPayerSkill(
        slug=slug,
        name=meta.get("name", slug),
        description=meta.get("description", ""),
        path=rel_path,
        content=raw,
    )


def list_all_skills() -> list[LoadedPayerSkill]:
    """List all installed payer audit skills from .agents/skills/."""
    settings = get_settings()
    results: list[LoadedPayerSkill] = []
    if not settings.skills_dir.exists():
        return results
    for skill_dir in sorted(settings.skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if skill_dir.is_dir() and skill_file.exists():
            raw = skill_file.read_text(encoding="utf-8")
            meta, _body = _parse_frontmatter(raw)
            slug = skill_dir.name
            results.append(
                LoadedPayerSkill(
                    slug=slug,
                    name=meta.get("name", slug),
                    description=meta.get("description", ""),
                    path=f".agents/skills/{slug}/SKILL.md",
                    content=raw,
                )
            )
    return results
