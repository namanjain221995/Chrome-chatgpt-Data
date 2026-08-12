"""Authorized-admin full-text search over archived message text.

Adds a stored generated ``tsvector`` column plus a GIN index so a compliance
administrator can search archived content from an authorized tool. There is no
public search endpoint; access is role-gated and audited (see docs/SECURITY.md).

Revision ID: 0002_fts
Revises: 0001_initial
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_fts"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_versions",
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', plain_text)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_message_versions_search_tsv",
        "message_versions",
        ["search_tsv"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_versions_search_tsv",
        table_name="message_versions",
        postgresql_using="gin",
    )
    op.drop_column("message_versions", "search_tsv")
