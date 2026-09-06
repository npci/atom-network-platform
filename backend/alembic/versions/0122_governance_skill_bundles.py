# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Governance skills become BUNDLES (Agent-Skill shape): archive + manifests.

Adds bundle columns to governance_skills. Existing single-.md rows keep working
as degenerate bundles (content = SKILL.md, bundle_bytes NULL, zero scripts —
which run pure-reasoning, exactly today's behaviour). A scripted bundle stores
the verbatim archive (audit: what was enforced is byte-pinned), its per-file
manifest, the per-script execution contracts, provenance, and the prove-it-runs
smoke status that gates whether its scripts may ever gate a change.

Idempotent + inspector-gated (safe to re-run).

Revision ID: 0121
Revises: 0120
"""
from alembic import op
import sqlalchemy as sa

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

_COLS = [
    ("bundle_bytes", sa.LargeBinary()),          # verbatim .tar.gz/.zip; NULL = md-only skill
    ("bundle_sha256", sa.String(64)),
    ("bundle_filename", sa.String(255)),
    ("manifest_json", sa.JSON()),                # [{path, bytes, sha256, classification}]
    ("exec_manifest_json", sa.JSON()),           # per-script execution contracts
    ("safety_warnings_json", sa.JSON()),         # static-gate capability warnings
    ("provenance_json", sa.JSON()),              # {source, repo, commit, tag}
    ("smoke_status", sa.String(16)),             # pending|green|failed; NULL = no scripts
    ("smoke_detail_json", sa.JSON()),
]


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "governance_skills" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("governance_skills")}
    for name, coltype in _COLS:
        if name not in cols:
            op.add_column("governance_skills", sa.Column(name, coltype, nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "governance_skills" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("governance_skills")}
    for name, _ in _COLS:
        if name in cols:
            op.drop_column("governance_skills", name)
