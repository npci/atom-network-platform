# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Kit publication snapshots + per-publication tagging on product kit docs.

`kit_publications` — immutable snapshot of the Product Kit envelope as shipped to
partners, one row per (change_request_id, negotiation_version). NPCI keeps the exact
bytes the partner negotiated against even after the working docs are regenerated.

`product_kit_documents.negotiation_version` — tags which published version a working
doc version was generated/cloned for.

Idempotent + inspector-gated . JSON columns use Postgres JSONB
with a SQLite JSON variant so the test harness doesn't break (see 0040/0044).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

_JSON = JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "kit_publications" not in set(insp.get_table_names()):
        op.create_table(
            "kit_publications",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "change_request_id", sa.String(36),
                sa.ForeignKey("change_requests.id"), nullable=False, index=True,
            ),
            sa.Column("negotiation_version", sa.Integer, nullable=False),
            sa.Column("envelope", _JSON, nullable=False),
            sa.Column("envelope_sha256", sa.String(64), nullable=False),
            sa.Column("source_doc_versions", _JSON, nullable=False),
            sa.Column("revision_reason", sa.Text, nullable=True),
            sa.Column("resolver_action", sa.String(50), nullable=True),
            sa.Column(
                "published_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "published_by", sa.String(36),
                sa.ForeignKey("users.id"), nullable=True,
            ),
            sa.UniqueConstraint(
                "change_request_id", "negotiation_version",
                name="uq_kit_publications_change_version",
            ),
        )

    pk_cols = {c["name"] for c in insp.get_columns("product_kit_documents")}
    if "negotiation_version" not in pk_cols:
        op.add_column(
            "product_kit_documents",
            sa.Column("negotiation_version", sa.Integer, nullable=False, server_default="1"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    pk_cols = {c["name"] for c in insp.get_columns("product_kit_documents")}
    if "negotiation_version" in pk_cols:
        op.drop_column("product_kit_documents", "negotiation_version")

    if "kit_publications" in set(insp.get_table_names()):
        op.drop_table("kit_publications")
