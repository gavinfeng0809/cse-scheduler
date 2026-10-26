"""[F3] 员工级夜班策略 + Shift.counts_as_night_streak

Revision ID: b1f7c2a90d31
Revises: a9a4094e1e6a
"""
from alembic import op
import sqlalchemy as sa


revision = "b1f7c2a90d31"
down_revision = "a9a4094e1e6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("night_max_consecutive", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "employees",
        sa.Column("night_rest_days", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "employees",
        sa.Column(
            "night_policy_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "shifts",
        sa.Column(
            "counts_as_night_streak",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # 连班只看 N：N=True，假日夜班 Y=False（业务确认）
    op.execute("UPDATE shifts SET counts_as_night_streak = (code = 'N')")


def downgrade() -> None:
    op.drop_column("shifts", "counts_as_night_streak")
    op.drop_column("employees", "night_policy_confirmed")
    op.drop_column("employees", "night_rest_days")
    op.drop_column("employees", "night_max_consecutive")
