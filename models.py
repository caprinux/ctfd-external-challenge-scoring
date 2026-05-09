import datetime

from CTFd.models import Challenges, db


class ExternalScoredChallenge(Challenges):
    __mapper_args__ = {"polymorphic_identity": "external_scored"}


class ExternalScoringLaunch(db.Model):
    __tablename__ = "external_scoring_launches"

    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False)
    created = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    expires = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("Users", foreign_keys="ExternalScoringLaunch.user_id", lazy="select")
    team = db.relationship("Teams", foreign_keys="ExternalScoringLaunch.team_id", lazy="select")
    challenge = db.relationship("Challenges", foreign_keys="ExternalScoringLaunch.challenge_id", lazy="select")

    def __repr__(self):
        return f"<ExternalScoringLaunch {self.jti}>"


class ExternalScore(db.Model):
    __tablename__ = "external_scores"
    __table_args__ = (db.UniqueConstraint("challenge_id", "team_id"), {})

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    best_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    solve_id = db.Column(db.Integer, db.ForeignKey("submissions.id", ondelete="SET NULL"), nullable=True)
    best_points = db.Column(db.Integer, default=0, nullable=False)
    created = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )

    challenge = db.relationship("Challenges", foreign_keys="ExternalScore.challenge_id", lazy="select")
    team = db.relationship("Teams", foreign_keys="ExternalScore.team_id", lazy="select")
    best_user = db.relationship("Users", foreign_keys="ExternalScore.best_user_id", lazy="select")
    solve = db.relationship("Submissions", foreign_keys="ExternalScore.solve_id", lazy="select")

    def __repr__(self):
        return f"<ExternalScore challenge_id={self.challenge_id} team_id={self.team_id} best_points={self.best_points}>"


class ExternalScoreEvent(db.Model):
    __tablename__ = "external_score_events"
    __table_args__ = (
        db.UniqueConstraint("challenge_id", "team_id", "idempotency_key"),
        {},
    )

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    points = db.Column(db.Integer, nullable=False)
    previous_best = db.Column(db.Integer, nullable=False)
    new_best = db.Column(db.Integer, nullable=False)
    delta_awarded = db.Column(db.Integer, nullable=False, default=0)
    award_id = db.Column(db.Integer, db.ForeignKey("awards.id", ondelete="SET NULL"), nullable=True)
    solve_id = db.Column(db.Integer, db.ForeignKey("submissions.id", ondelete="SET NULL"), nullable=True)
    idempotency_key = db.Column(db.String(128), nullable=False)
    provided = db.Column(db.Text, nullable=True)
    details = db.Column(db.JSON, nullable=True)
    created = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    challenge = db.relationship("Challenges", foreign_keys="ExternalScoreEvent.challenge_id", lazy="select")
    team = db.relationship("Teams", foreign_keys="ExternalScoreEvent.team_id", lazy="select")
    user = db.relationship("Users", foreign_keys="ExternalScoreEvent.user_id", lazy="select")
    award = db.relationship("Awards", foreign_keys="ExternalScoreEvent.award_id", lazy="select")
    solve = db.relationship("Submissions", foreign_keys="ExternalScoreEvent.solve_id", lazy="select")

    def __repr__(self):
        return f"<ExternalScoreEvent challenge_id={self.challenge_id} team_id={self.team_id} points={self.points}>"
