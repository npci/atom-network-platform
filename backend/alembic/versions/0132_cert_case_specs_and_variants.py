"""cert_case_specs + cert_request_variants — the graded certification scope (CERT-1, §3.1)

Revision ID: 0132
Revises: 0131
Create Date: 2026-08-30

Two tables, two different nouns:

* `cert_request_variants` — one row per EXECUTABLE input combination for a
  case (§3.1). A variant executes once; a materially different combination is
  a different variant. `variant_id` is a deterministic content hash so the
  same registry snapshot + rules + test data rebuild identical rows.
* `cert_case_specs` — one row per ASSERTION evaluated against a variant's
  captured payload. Many specs share one execution: a forty-field message is
  forty-odd assertion rows and ONE simulator transaction.

`expected` and `field_path` are COPIED from the registry at generation time —
never referenced — so a mid-cert registry edit cannot retroactively change
what a round asserted. `wire_format` snapshots the pack's codec key per row;
evaluation resolves the codec from the row, keeping stored rounds
reproducible. Neutral vocabulary throughout: `initiator` stores
authority|partner (the wire's npci|bank is mapped at the wire boundary) and
the spec's `npci_data` column lands here as `authority_data`.

Additive only. Idempotent + inspector-gated (repo convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "cert_request_variants" not in tables:
        op.create_table(
            "cert_request_variants",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("cflow_id", sa.String(64), nullable=False),
            sa.Column("run_number", sa.Integer(), nullable=False),
            sa.Column("case_id", sa.String(100), nullable=False),
            sa.Column("variant_id", sa.String(64), nullable=False),
            sa.Column("api_message_id", sa.String(36),
                      sa.ForeignKey("api_messages.id"), nullable=True),
            sa.Column("initiator", sa.String(10), nullable=False,
                      server_default="authority"),
            sa.Column("wire_format", sa.String(20), nullable=False,
                      server_default="xml"),
            sa.Column("input_data", sa.JSON(), nullable=True),
            sa.Column("fixture_ref", sa.String(200), nullable=True),
            sa.Column("expected", sa.JSON(), nullable=False),
            sa.Column("strategy", sa.String(30), nullable=False),
            sa.Column("covered_rules", sa.JSON(), nullable=True),
            sa.Column("is_negative", sa.Boolean(), nullable=False,
                      server_default=sa.text("false")),
            sa.Column("fault_key", sa.String(200), nullable=True),
            sa.Column("provenance", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            # case_id is PART OF THE KEY. A variant is identified within its
            # case — two catalogue cases with identical request inputs are
            # different executions, and keying without case_id made them
            # collide and abort the round.
            sa.UniqueConstraint("cflow_id", "run_number", "case_id", "variant_id",
                                name="uq_cert_request_variants_round_case_variant"),
        )

    if "cert_case_specs" not in tables:
        op.create_table(
            "cert_case_specs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("cflow_id", sa.String(64), nullable=False),
            sa.Column("run_number", sa.Integer(), nullable=False),
            sa.Column("case_id", sa.String(100), nullable=False),
            sa.Column("variant_id", sa.String(36),
                      sa.ForeignKey("cert_request_variants.id"), nullable=True),
            sa.Column("api_message_id", sa.String(36),
                      sa.ForeignKey("api_messages.id"), nullable=True),
            sa.Column("api_field_id", sa.String(36),
                      sa.ForeignKey("api_fields.id"), nullable=True),
            sa.Column("field_path", sa.String(1000), nullable=True),
            sa.Column("assertion_kind", sa.String(20), nullable=False),
            sa.Column("expected", sa.JSON(), nullable=False),
            sa.Column("origin", sa.String(20), nullable=False),
            sa.Column("wire_format", sa.String(20), nullable=False,
                      server_default="xml"),
            sa.Column("authority_data", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    insp = sa.inspect(bind)  # re-inspect: tables may have just been created
    variant_idx = {i["name"] for i in insp.get_indexes("cert_request_variants")}
    if "ix_cert_request_variants_round_case" not in variant_idx:
        op.create_index("ix_cert_request_variants_round_case",
                        "cert_request_variants",
                        ["cflow_id", "run_number", "case_id"])
    spec_idx = {i["name"] for i in insp.get_indexes("cert_case_specs")}
    if "ix_cert_case_specs_round_case" not in spec_idx:
        op.create_index("ix_cert_case_specs_round_case", "cert_case_specs",
                        ["cflow_id", "run_number", "case_id"])


def downgrade():
    op.drop_table("cert_case_specs")
    op.drop_table("cert_request_variants")
