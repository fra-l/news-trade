"""drop watchlist_selections table

Revision ID: a1b2c3d4e5f6
Revises: 6dae9e7efe75
Create Date: 2026-04-09 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "6dae9e7efe75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("watchlist_selections")


def downgrade() -> None:
    op.create_table(
        "watchlist_selections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tickers_json", sa.Text(), nullable=False),
        sa.Column("saved_at", sa.DateTime(), nullable=False),
    )
