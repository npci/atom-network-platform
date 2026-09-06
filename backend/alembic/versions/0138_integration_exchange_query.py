"""integration_exchanges.query — record the query string on each tunnelled hop

Revision ID: 0138
Revises: 0137
Create Date: 2026-09-02

Found during the two-sided integration run against the partner platform
(NET-F21). Four live exchanges carrying `?pack=CHG-4711%403`,
`?pack=baseline@demo`, `?a=1&a=2` and no query at all produced four rows that
were IDENTICAL in every recorded column — path=/api/health, status=200. The
telemetry could not distinguish them.

That matters because Stage 5 contract selection rides entirely on `?pack=`, and
the documented failure mode is a normalised or dropped query presenting later as
"certified against baseline" rather than as an error. The tunnel's own audit
trail was blind to the one field that failure lives in, which defeats the
"a failed exchange must be diagnosable from that row alone" criterion for the
highest-consequence case.

The query is already on the wire (`integration_contract.encode_request` emits
`request.query` and the string is carried opaquely, never parsed) — it was only
being dropped at the recording step. So this is additive observability, not a
protocol change, and needs no coordinated release with the partner.

One nullable string column: NULL on legacy rows, "" for a hop that genuinely
carried no query — those are different facts and the column distinguishes them.
Sized to match `path`. Additive only. Idempotent + inspector-gated (repo
convention).
"""
from alembic import op
import sqlalchemy as sa

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {c["name"] for c in insp.get_columns("integration_exchanges")}
    if "query" not in existing:
        op.add_column(
            "integration_exchanges",
            sa.Column("query", sa.String(length=1000), nullable=True),
        )


def downgrade():
    op.drop_column("integration_exchanges", "query")
