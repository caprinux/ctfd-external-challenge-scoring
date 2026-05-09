import datetime
import json
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Blueprint, abort, redirect, request
from sqlalchemy.exc import IntegrityError

from CTFd.cache import clear_challenges, clear_standings
from CTFd.models import Awards, Challenges, Solves, Teams, Users, db
from CTFd.plugins import register_plugin_assets_directory
from CTFd.exceptions.challenges import (
    ChallengeCreateException,
    ChallengeUpdateException,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES, BaseChallenge
from CTFd.plugins.migrations import upgrade
from CTFd.utils import get_config
from CTFd.utils.config import is_teams_mode
from CTFd.utils.dates import ctf_paused, ctftime, isoformat
from CTFd.utils.decorators import admins_only, authed_only, require_verified_emails
from CTFd.utils.security.signing import (
    BadSignature,
    SignatureExpired,
    serialize,
    unserialize,
)
from CTFd.utils.user import get_current_team, get_current_user, get_ip

from .models import ExternalScoredChallenge, ExternalScore, ExternalScoreEvent
from .models import ExternalScoringLaunch


CHALLENGE_TYPE = "external_scored"
LAUNCH_TOKEN_MAX_AGE = 300
IDEMPOTENCY_KEY_MAX_LENGTH = 128
MAX_POINTS = 2147483647
MAX_PROVIDED_LENGTH = 4096
MAX_DETAILS_JSON_LENGTH = 65535

external_scoring = Blueprint("external_scoring", __name__)
external_scoring_api = Blueprint(
    "external_scoring_api", __name__, url_prefix="/api/v1/external-scoring"
)


class ExternalScoredChallengeType(BaseChallenge):
    id = CHALLENGE_TYPE
    name = "External Scored"
    templates = {
        "create": "/plugins/external_scoring/assets/create.html",
        "update": "/plugins/external_scoring/assets/update.html",
        "view": "/plugins/external_scoring/assets/view.html",
    }
    scripts = {
        "create": "/plugins/external_scoring/assets/create.js",
        "update": "/plugins/external_scoring/assets/update.js",
        "view": "/plugins/external_scoring/assets/view.js",
    }
    route = "/plugins/external_scoring/assets/"
    challenge_model = ExternalScoredChallenge

    @classmethod
    def create(cls, req):
        data = dict(req.form or req.get_json() or {})
        data["type"] = cls.id
        data["value"] = 0
        data["function"] = "static"
        external_url = (data.get("connection_info") or "").strip()
        if _valid_external_url(external_url) is False:
            raise ChallengeCreateException(
                "External Challenge URL must be an absolute http(s) URL"
            )
        data["connection_info"] = external_url

        challenge = cls.challenge_model(**data)
        db.session.add(challenge)
        db.session.commit()
        return challenge

    @classmethod
    def update(cls, challenge, req):
        data = dict(req.form or req.get_json() or {})
        data["value"] = 0
        data["function"] = "static"
        external_url = (data.get("connection_info") or "").strip()
        if _valid_external_url(external_url) is False:
            raise ChallengeUpdateException(
                "External Challenge URL must be an absolute http(s) URL"
            )
        data["connection_info"] = external_url

        for attr, value in data.items():
            if attr == "type":
                continue
            setattr(challenge, attr, value)

        challenge.type = cls.id
        challenge.value = 0
        challenge.function = "static"
        db.session.commit()
        return challenge

    @classmethod
    def read(cls, challenge):
        data = super().read(challenge)
        data["value"] = 0
        return data

    @classmethod
    def delete(cls, challenge):
        award_ids = [
            award_id
            for (award_id,) in ExternalScoreEvent.query.with_entities(
                ExternalScoreEvent.award_id
            ).filter_by(challenge_id=challenge.id)
            if award_id is not None
        ]
        if award_ids:
            Awards.query.filter(Awards.id.in_(award_ids)).delete(
                synchronize_session=False
            )

        ExternalScoreEvent.query.filter_by(challenge_id=challenge.id).delete()
        ExternalScore.query.filter_by(challenge_id=challenge.id).delete()
        ExternalScoringLaunch.query.filter_by(challenge_id=challenge.id).delete()
        db.session.commit()

        super().delete(challenge)


def _json_error(message, status=400, field=""):
    if isinstance(message, dict):
        errors = message
    else:
        errors = {field: [message]}
    return {"success": False, "errors": errors}, status


def _valid_external_url(url):
    if not isinstance(url, str) or not url:
        return False
    if any(ch.isspace() for ch in url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _now():
    return datetime.datetime.utcnow()


def _freeze_time_passed():
    freeze = get_config("freeze")
    if not freeze:
        return False
    try:
        return time.time() >= int(freeze)
    except (TypeError, ValueError):
        return False


def _scoring_closed_reason():
    if ctftime() is False:
        return "CTF is not active"
    if ctf_paused():
        return "CTF is paused"
    if _freeze_time_passed():
        return "Score submissions are closed because the scoreboard is frozen"
    return None


def _ensure_teams_mode_json():
    if is_teams_mode() is False:
        return _json_error("External scoring is only supported in teams mode", 400)
    return None


def _get_external_challenge_or_404(challenge_id):
    challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()
    if challenge.type != CHALLENGE_TYPE:
        abort(404)
    return challenge


def _challenge_closed_reason(challenge):
    if challenge.state == "hidden":
        return "Challenge is hidden"
    if challenge.state == "locked":
        return "Challenge is locked"
    return None


def _user_has_prerequisites(user, challenge):
    if not challenge.requirements:
        return True

    requirements = challenge.requirements.get("prerequisites", [])
    if not requirements:
        return True

    solve_ids = (
        Solves.query.with_entities(Solves.challenge_id)
        .filter_by(account_id=user.account_id)
        .order_by(Solves.challenge_id.asc())
        .all()
    )
    solve_ids = {solve_id for (solve_id,) in solve_ids}
    all_challenge_ids = {
        c.id for c in Challenges.query.with_entities(Challenges.id).all()
    }
    prereqs = set(requirements).intersection(all_challenge_ids)
    return solve_ids >= prereqs


def _ensure_launch_allowed_or_abort(challenge, user, team):
    if is_teams_mode() is False:
        abort(403, description="External scoring is only supported in teams mode")
    if team is None:
        abort(403, description="You must be on a team to launch this challenge")

    reason = _scoring_closed_reason()
    if reason:
        abort(403, description=reason)

    reason = _challenge_closed_reason(challenge)
    if reason == "Challenge is hidden":
        abort(404)
    if reason:
        abort(403, description=reason)

    if _user_has_prerequisites(user, challenge) is False:
        abort(403, description="Challenge prerequisites are not satisfied")


def _append_launch_token(external_url, token):
    parts = urlsplit(external_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("ctfd_launch_token", token))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _create_launch_token(user, team, challenge):
    jti = os.urandom(32).hex()
    expires = _now() + datetime.timedelta(seconds=LAUNCH_TOKEN_MAX_AGE)
    launch = ExternalScoringLaunch(
        jti=jti,
        user_id=user.id,
        team_id=team.id,
        challenge_id=challenge.id,
        expires=expires,
    )
    db.session.add(launch)
    db.session.commit()

    token = serialize(
        {
            "jti": jti,
            "user_id": user.id,
            "team_id": team.id,
            "challenge_id": challenge.id,
        }
    )
    return token


def _serialize_event(event):
    return {
        "id": event.id,
        "challenge_id": event.challenge_id,
        "team_id": event.team_id,
        "user_id": event.user_id,
        "user_name": event.user.name if event.user else None,
        "points": event.points,
        "previous_best": event.previous_best,
        "new_best": event.new_best,
        "delta_awarded": event.delta_awarded,
        "award_id": event.award_id,
        "solve_id": event.solve_id,
        "idempotency_key": event.idempotency_key,
        "provided": event.provided,
        "details": event.details,
        "created": isoformat(event.created),
    }


def _serialize_score(score, challenge_id, team_id):
    return {
        "challenge_id": challenge_id,
        "team_id": team_id,
        "best_points": score.best_points if score else 0,
        "best_user_id": score.best_user_id if score else None,
        "solve_id": score.solve_id if score else None,
    }


def _parse_required_int(data, field):
    value = data.get(field)
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be an integer") from e


def _parse_points(data):
    points = _parse_required_int(data, "points")
    if points < 0:
        raise ValueError("points must be a non-negative integer")
    if points > MAX_POINTS:
        raise ValueError(f"points must be less than or equal to {MAX_POINTS}")
    return points


def _validate_details(details):
    if details is None:
        return None
    try:
        encoded = json.dumps(details, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise ValueError("details must be JSON serializable") from e
    if len(encoded.encode("utf-8")) > MAX_DETAILS_JSON_LENGTH:
        raise ValueError(
            f"details JSON must be at most {MAX_DETAILS_JSON_LENGTH} bytes"
        )
    return details


def _get_or_create_score_record(challenge_id, team_id):
    score = ExternalScore.query.filter_by(
        challenge_id=challenge_id, team_id=team_id
    ).first()
    if score is None:
        score = ExternalScore(
            challenge_id=challenge_id,
            team_id=team_id,
            best_points=0,
        )
        db.session.add(score)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()

    return (
        ExternalScore.query.filter_by(challenge_id=challenge_id, team_id=team_id)
        .with_for_update()
        .first()
    )


def _get_or_create_solve(challenge, user, team, provided):
    solve = Solves.query.filter_by(
        challenge_id=challenge.id,
        team_id=team.id,
    ).first()
    if solve:
        return solve

    solve = Solves(
        user_id=user.id,
        team_id=team.id,
        challenge_id=challenge.id,
        ip=get_ip(req=request),
        provided=provided or "External scoring submission",
    )
    db.session.add(solve)
    db.session.flush()
    return solve


def _load_score_history(challenge_id, team_id):
    score = ExternalScore.query.filter_by(
        challenge_id=challenge_id,
        team_id=team_id,
    ).first()
    events = (
        ExternalScoreEvent.query.filter_by(
            challenge_id=challenge_id,
            team_id=team_id,
        )
        .order_by(ExternalScoreEvent.created.desc(), ExternalScoreEvent.id.desc())
        .all()
    )
    return score, events


def get_team_external_score(challenge_id):
    team = get_current_team()
    if team is None:
        return None
    return ExternalScore.query.filter_by(
        challenge_id=challenge_id,
        team_id=team.id,
    ).first()


def get_team_external_score_events(challenge_id):
    team = get_current_team()
    if team is None:
        return []
    return (
        ExternalScoreEvent.query.filter_by(
            challenge_id=challenge_id,
            team_id=team.id,
        )
        .order_by(ExternalScoreEvent.created.desc(), ExternalScoreEvent.id.desc())
        .all()
    )


@external_scoring.route("/external-scoring/launch/<int:challenge_id>")
@require_verified_emails
@authed_only
def launch(challenge_id):
    user = get_current_user()
    team = get_current_team()
    challenge = _get_external_challenge_or_404(challenge_id)
    _ensure_launch_allowed_or_abort(challenge, user, team)

    external_url = (challenge.connection_info or "").strip()
    if _valid_external_url(external_url) is False:
        abort(500, description="External challenge URL is not configured correctly")

    token = _create_launch_token(user=user, team=team, challenge=challenge)
    return redirect(_append_launch_token(external_url, token))


@external_scoring_api.route("/launch/verify", methods=["POST"])
@admins_only
def verify_launch():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    if not token:
        return _json_error("token is required", 400, "token")

    try:
        payload = unserialize(token, max_age=LAUNCH_TOKEN_MAX_AGE)
    except SignatureExpired:
        return _json_error("launch token has expired", 400, "token")
    except BadSignature:
        return _json_error("launch token is invalid", 400, "token")

    jti = payload.get("jti")
    launch = ExternalScoringLaunch.query.filter_by(jti=jti).with_for_update().first()
    if launch is None:
        return _json_error("launch token is unknown", 400, "token")
    if launch.used:
        return _json_error("launch token has already been used", 400, "token")
    if launch.expires < _now():
        return _json_error("launch token has expired", 400, "token")

    try:
        user_id = int(payload.get("user_id"))
        team_id = int(payload.get("team_id"))
        challenge_id = int(payload.get("challenge_id"))
    except (TypeError, ValueError):
        return _json_error("launch token payload is invalid", 400, "token")

    if (
        launch.user_id != user_id
        or launch.team_id != team_id
        or launch.challenge_id != challenge_id
    ):
        return _json_error("launch token payload does not match stored launch", 400)

    reason = _scoring_closed_reason()
    if reason:
        return _json_error(reason, 403)

    challenge = Challenges.query.filter_by(id=challenge_id).first()
    if challenge is None or challenge.type != CHALLENGE_TYPE:
        return _json_error("challenge is invalid", 400, "challenge_id")
    reason = _challenge_closed_reason(challenge)
    if reason:
        return _json_error(reason, 403)

    user = Users.query.filter_by(id=user_id).first()
    team = Teams.query.filter_by(id=team_id).first()
    if user is None or team is None:
        return _json_error("user or team no longer exists", 400)
    if user.team_id != team.id:
        return _json_error("user is no longer on the launched team", 400)
    if user.banned or team.banned:
        return _json_error("user or team is banned", 403)

    launch.used = True
    launch.used_at = _now()
    db.session.commit()

    return {
        "success": True,
        "data": {
            "user_id": user.id,
            "team_id": team.id,
            "challenge_id": challenge.id,
            "user_name": user.name,
            "team_name": team.name,
        },
    }


@external_scoring_api.route("/challenges/<int:challenge_id>/score", methods=["POST"])
@admins_only
def submit_score(challenge_id):
    teams_mode_error = _ensure_teams_mode_json()
    if teams_mode_error:
        return teams_mode_error

    reason = _scoring_closed_reason()
    if reason:
        return _json_error(reason, 403)

    data = request.get_json(silent=True) or {}
    try:
        user_id = _parse_required_int(data, "user_id")
        team_id = _parse_required_int(data, "team_id")
        points = _parse_points(data)
    except ValueError as e:
        return _json_error(str(e), 400)

    idempotency_key = data.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return _json_error(
            "idempotency_key is required", 400, "idempotency_key"
        )
    idempotency_key = idempotency_key.strip()
    if len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        return _json_error(
            f"idempotency_key must be at most {IDEMPOTENCY_KEY_MAX_LENGTH} characters",
            400,
            "idempotency_key",
        )

    challenge = Challenges.query.filter_by(id=challenge_id).first()
    if challenge is None or challenge.type != CHALLENGE_TYPE:
        return _json_error("challenge does not exist", 404, "challenge_id")
    reason = _challenge_closed_reason(challenge)
    if reason:
        return _json_error(reason, 403)

    user = Users.query.filter_by(id=user_id).first()
    team = Teams.query.filter_by(id=team_id).first()
    if user is None:
        return _json_error("user does not exist", 404, "user_id")
    if team is None:
        return _json_error("team does not exist", 404, "team_id")
    if user.team_id != team.id:
        return _json_error("user is not on the specified team", 400)
    if user.banned or team.banned:
        return _json_error("user or team is banned", 403)

    existing_event = ExternalScoreEvent.query.filter_by(
        challenge_id=challenge.id,
        team_id=team.id,
        idempotency_key=idempotency_key,
    ).first()
    if existing_event:
        score, _events = _load_score_history(challenge.id, team.id)
        return {
            "success": True,
            "data": {
                "idempotent_replay": True,
                "score": _serialize_score(score, challenge.id, team.id),
                "event": _serialize_event(existing_event),
            },
        }

    provided = data.get("provided")
    if provided is not None:
        provided = str(provided)
        if len(provided) > MAX_PROVIDED_LENGTH:
            return _json_error(
                f"provided must be at most {MAX_PROVIDED_LENGTH} characters",
                400,
                "provided",
            )
    try:
        details = _validate_details(data.get("details"))
    except ValueError as e:
        return _json_error(str(e), 400, "details")

    score = _get_or_create_score_record(challenge.id, team.id)
    previous_best = int(score.best_points or 0)
    new_best = previous_best
    delta_awarded = 0
    award = None

    try:
        solve = _get_or_create_solve(challenge, user, team, provided)

        if points > previous_best:
            delta_awarded = points - previous_best
            new_best = points
            award = Awards(
                user_id=user.id,
                team_id=team.id,
                name=f"External score: {challenge.name}"[:80],
                description=(
                    f"External score for {challenge.name}: "
                    f"{previous_best} -> {new_best} (+{delta_awarded})"
                ),
                value=delta_awarded,
                category="external_scoring",
            )
            db.session.add(award)
            db.session.flush()

            score.best_points = new_best
            score.best_user_id = user.id
        else:
            if score.best_user_id is None:
                score.best_user_id = user.id

        score.solve_id = solve.id

        event = ExternalScoreEvent(
            challenge_id=challenge.id,
            team_id=team.id,
            user_id=user.id,
            points=points,
            previous_best=previous_best,
            new_best=new_best,
            delta_awarded=delta_awarded,
            award_id=award.id if award else None,
            solve_id=solve.id,
            idempotency_key=idempotency_key,
            provided=provided,
            details=details,
        )
        db.session.add(event)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing_event = ExternalScoreEvent.query.filter_by(
            challenge_id=challenge.id,
            team_id=team.id,
            idempotency_key=idempotency_key,
        ).first()
        if existing_event:
            score, _events = _load_score_history(challenge.id, team.id)
            return {
                "success": True,
                "data": {
                    "idempotent_replay": True,
                    "score": _serialize_score(score, challenge.id, team.id),
                    "event": _serialize_event(existing_event),
                },
            }
        return _json_error("could not record score", 500)

    clear_standings()
    clear_challenges()

    return {
        "success": True,
        "data": {
            "idempotent_replay": False,
            "score": _serialize_score(score, challenge.id, team.id),
            "event": _serialize_event(event),
        },
    }


@external_scoring_api.route("/challenges/<int:challenge_id>/score/me", methods=["GET"])
@authed_only
def get_my_score(challenge_id):
    teams_mode_error = _ensure_teams_mode_json()
    if teams_mode_error:
        return teams_mode_error

    team = get_current_team()
    if team is None:
        return _json_error("You must be on a team", 403)

    user = get_current_user()
    challenge = Challenges.query.filter_by(id=challenge_id).first()
    if challenge is None or challenge.type != CHALLENGE_TYPE:
        return _json_error("challenge does not exist", 404, "challenge_id")
    reason = _challenge_closed_reason(challenge)
    if reason == "Challenge is hidden":
        return _json_error("challenge does not exist", 404, "challenge_id")
    if reason:
        return _json_error(reason, 403)
    if _user_has_prerequisites(user, challenge) is False:
        return _json_error("Challenge prerequisites are not satisfied", 403)

    score, events = _load_score_history(challenge.id, team.id)
    return {
        "success": True,
        "data": {
            "score": _serialize_score(score, challenge.id, team.id),
            "events": [_serialize_event(event) for event in events],
        },
    }


def load(app):
    upgrade(plugin_name="external_scoring")
    CHALLENGE_CLASSES[CHALLENGE_TYPE] = ExternalScoredChallengeType
    register_plugin_assets_directory(
        app, base_path="/plugins/external_scoring/assets/"
    )
    app.register_blueprint(external_scoring)
    app.register_blueprint(external_scoring_api)
    app.jinja_env.globals.update(
        external_scoring_get_team_score=get_team_external_score,
        external_scoring_get_team_events=get_team_external_score_events,
    )
