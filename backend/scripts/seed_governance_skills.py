"""Seed the example EA + InfoSec governance skills from examples/governance_skills/.

Loads the two generic example rulebooks into the `governance_skills` table so
the pre-build governance stages (EA Review → InfoSec Review) can run without a
manual admin upload — the same append-only versioning the upload API uses.

Idempotent: if a slot's newest row already carries the file's exact checksum,
nothing is inserted. An edited file seeds as a NEW version (append-only, like
the UI). Nothing is ever updated or deleted.

Run natively from backend/ (`python scripts/seed_governance_skills.py`) or in
docker (`docker compose run --rm backend python scripts/seed_governance_skills.py`).
Remember: the stages also need GOVERNANCE_REVIEWS_ENABLED=true.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.governance_skills import validate_skill          # noqa: E402
from app.api.governance import _slot_name, active_skill          # noqa: E402
from app.core.database import SessionLocal                       # noqa: E402
from app.models.governance_skill import GovernanceSkill          # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance_skills"
FILES = {
    "ea":      EXAMPLES / "ea_review_skill.md",
    "infosec": EXAMPLES / "infosec_review_skill.md",
}


def seed() -> int:
    db = SessionLocal()
    rc = 0
    try:
        for stype, path in FILES.items():
            if not path.is_file():
                print(f"[{stype}] MISSING example file: {path}", file=sys.stderr)
                rc = 1
                continue
            content = path.read_text(encoding="utf-8")
            try:
                parsed = validate_skill(content)
            except ValueError as e:
                print(f"[{stype}] example does not parse — not seeded: {e}", file=sys.stderr)
                rc = 1
                continue
            slot = _slot_name(parsed.get("name", ""), path.name)
            newest_in_slot = (
                db.query(GovernanceSkill)
                .filter(GovernanceSkill.skill_type == stype,
                        GovernanceSkill.name == slot)
                .order_by(GovernanceSkill.version.desc())
                .first()
            )
            if newest_in_slot is not None and newest_in_slot.checksum == parsed["checksum"]:
                print(f"[{stype}] slot '{slot}' already at this content "
                      f"(v{newest_in_slot.version}, {parsed['checksum'][:12]}…) — skipped")
                continue
            prev = active_skill(db, stype)           # global per-type numbering
            row = GovernanceSkill(
                skill_type=stype,
                version=(prev.version + 1) if prev else 1,
                name=slot,
                content=content,
                checksum=parsed["checksum"],
                filename=path.name,
                rules_json=parsed["rules"],
                uploaded_by=None,                     # seeded, not user-uploaded
                provenance_json={"source": "seed_governance_skills.py",
                                 "example": True, "filename": path.name},
            )
            db.add(row)
            db.commit()
            print(f"[{stype}] seeded slot '{slot}' as v{row.version} "
                  f"({len(parsed['rules'])} rules, mode={parsed['mode']})")
    finally:
        db.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(seed())
