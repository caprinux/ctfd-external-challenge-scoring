"""Create external scoring tables

Revision ID: 9f7b8a6c5d4e
Revises: None
Create Date: 2026-05-09

"""
import sqlalchemy as sa

from CTFd.plugins.migrations import get_all_tables

revision = "9f7b8a6c5d4e"
down_revision = None
branch_labels = None
depends_on = None


def upgrade(op=None):
    tables = get_all_tables(op)

    if "external_scoring_launches" not in tables:
        op.create_table(
            "external_scoring_launches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("jti", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("challenge_id", sa.Integer(), nullable=False),
            sa.Column("created", sa.DateTime(), nullable=False),
            sa.Column("expires", sa.DateTime(), nullable=False),
            sa.Column("used", sa.Boolean(), nullable=False, default=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["challenge_id"], ["challenges.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint("jti", name="uq_external_scoring_launches_jti"),
        )

    if "external_scores" not in tables:
        op.create_table(
            "external_scores",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("challenge_id", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("best_user_id", sa.Integer(), nullable=True),
            sa.Column("solve_id", sa.Integer(), nullable=True),
            sa.Column("best_points", sa.Integer(), nullable=False, default=0),
            sa.Column("created", sa.DateTime(), nullable=False),
            sa.Column("updated", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["challenge_id"], ["challenges.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["best_user_id"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["solve_id"], ["submissions.id"], ondelete="SET NULL"
            ),
            sa.UniqueConstraint(
                "challenge_id", "team_id", name="uq_external_scores_challenge_team"
            ),
        )

    if "external_score_events" not in tables:
        op.create_table(
            "external_score_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("challenge_id", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("points", sa.Integer(), nullable=False),
            sa.Column("previous_best", sa.Integer(), nullable=False),
            sa.Column("new_best", sa.Integer(), nullable=False),
            sa.Column("delta_awarded", sa.Integer(), nullable=False, default=0),
            sa.Column("award_id", sa.Integer(), nullable=True),
            sa.Column("solve_id", sa.Integer(), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("provided", sa.Text(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("created", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["challenge_id"], ["challenges.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["award_id"], ["awards.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["solve_id"], ["submissions.id"], ondelete="SET NULL"
            ),
            sa.UniqueConstraint(
                "challenge_id",
                "team_id",
                "idempotency_key",
                name="uq_external_score_events_idempotency",
            ),
        )


def downgrade(op=None):
    tables = get_all_tables(op)
    if "external_score_events" in tables:
        op.drop_table("external_score_events")
    if "external_scores" in tables:
        op.drop_table("external_scores")
    if "external_scoring_launches" in tables:
        op.drop_table("external_scoring_launches")
