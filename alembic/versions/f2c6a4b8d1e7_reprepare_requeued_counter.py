"""reprepare_requeued_counter

Autopilot finding (2026-09-22): PREPARE items parked on "needs_user_input"
were never retried after the person approved the missing answers. The
scheduler now re-queues them, and reports how many under a new counter on
``scheduler_runs``.

Revision ID: f2c6a4b8d1e7
Revises: e4b2d8f0a6c3
Create Date: 2026-09-22 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c6a4b8d1e7"
down_revision: Union[str, None] = "e4b2d8f0a6c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scheduler_runs") as batch:
        batch.add_column(sa.Column("reprepare_requeued", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("scheduler_runs") as batch:
        batch.drop_column("reprepare_requeued")
