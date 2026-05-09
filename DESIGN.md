# External Scoring / Variable Points CTFd Plugin Design

## Goal

Develop a CTFd plugin that supports externally-scored challenges in CTFd teams mode. A challenge may be solved by interacting with an external challenge server, and the external server can report an integer score back to CTFd. The team's best score for that challenge determines the points awarded to the team.

This supports challenges such as ML/data-science challenges where submissions receive variable points.

## Core CTFd Integration Strategy

CTFd does not natively support a per-team/per-user point value on `Solves`. A normal solve receives the global `Challenges.value`. Therefore this plugin uses:

- `Solves` to mark the challenge as solved.
- `Awards` to award variable points.
- The challenge's global `Challenges.value` is forced to `0`.

This works with CTFd's existing scoreboard because CTFd score calculation already sums challenge solve values and award values.

For an external scored challenge:

- a `0` point external score still creates a solve,
- a positive score creates a solve and an award delta,
- later better scores create additional delta awards,
- lower/equal scores are logged but award no additional points.

Example:

| Submitted score | Previous best | Award delta | New best |
| --- | ---: | ---: | ---: |
| 0 | none/0 | 0 | 0 |
| 40 | 0 | +40 | 40 |
| 55 | 40 | +15 | 55 |
| 50 | 55 | +0 | 55 |

## Challenge Type

The plugin registers a new challenge type:

```text
external_scored
```

Admin behavior:

- admins create an `External Scored` challenge,
- value is forced to `0`,
- an external challenge URL is configured using `connection_info`,
- normal flag submission is not used.

User behavior:

- challenge card shows value `0`,
- challenge popup/window shows:
  - team best score,
  - a launch link,
  - all past score submissions for the team.

The main challenge board card remains at `0` for v1. Personalized best-score display is handled inside the challenge popup/window.

## Launch Token Flow

The external challenge server does not consume the CTFd auth cookie. Instead, CTFd issues a short-lived, single-use launch token.

### User Launch

User clicks a link in the challenge popup/window:

```http
GET /external-scoring/launch/<challenge_id>
```

CTFd validates:

- user is authenticated,
- CTFd is in teams mode,
- user belongs to a team,
- challenge exists and is `external_scored`,
- challenge is visible/unlocked,
- CTF is currently active,
- CTF is not paused,
- freeze time has not passed.

CTFd creates a single-use launch record and redirects to:

```text
<external_url>?ctfd_launch_token=<token>
```

The token expires after 5 minutes.

### External Server Launch Verification

The external challenge server calls:

```http
POST /api/v1/external-scoring/launch/verify
Authorization: Bearer <admin-token>
Content-Type: application/json
```

Body:

```json
{
  "token": "..."
}
```

Successful response:

```json
{
  "success": true,
  "data": {
    "user_id": 12,
    "team_id": 3,
    "challenge_id": 5,
    "user_name": "alice",
    "team_name": "blue-team"
  }
}
```

Verification consumes the token permanently. The external challenge server should create its own local session after verification.

## Score Submission API

External server submits scores using a CTFd admin token:

```http
POST /api/v1/external-scoring/challenges/<challenge_id>/score
Authorization: Bearer <admin-token>
Content-Type: application/json
```

Body:

```json
{
  "user_id": 12,
  "team_id": 3,
  "points": 55,
  "idempotency_key": "uuid-from-external-server",
  "provided": "accuracy=0.9123",
  "details": {
    "accuracy": 0.9123
  }
}
```

`idempotency_key` is required. It uniquely identifies one external scoring attempt and prevents duplicate processing if the external server retries a request.

Score submission validates:

- admin authentication,
- CTF is active,
- CTF is not paused,
- freeze time has not passed,
- challenge exists and is `external_scored`,
- user exists,
- team exists,
- user is still on that team,
- points is a non-negative integer,
- idempotency key has not already been processed for that team/challenge.

Behavior:

- always logs the submitted score event,
- creates a CTFd solve if the team has not already solved the challenge,
- awards only the positive delta over the previous best,
- clears CTFd standings/challenge caches.

## User Score/History API

The plugin exposes a user-authenticated API for the current team's score history:

```http
GET /api/v1/external-scoring/challenges/<challenge_id>/score/me
```

Response includes:

- current team best score,
- all submitted score events for the team/challenge.

## Database Tables

### `external_scoring_launches`

Stores single-use launch tokens.

Columns:

- `id`
- `jti`
- `user_id`
- `team_id`
- `challenge_id`
- `created`
- `expires`
- `used`
- `used_at`

Constraints:

- `jti` unique.

### `external_scores`

One row per team/challenge storing current best score.

Columns:

- `id`
- `challenge_id`
- `team_id`
- `best_user_id`
- `solve_id`
- `best_points`
- `created`
- `updated`

Constraints:

- unique `(challenge_id, team_id)`.

### `external_score_events`

All external score submissions.

Columns:

- `id`
- `challenge_id`
- `team_id`
- `user_id`
- `points`
- `previous_best`
- `new_best`
- `delta_awarded`
- `award_id`
- `solve_id`
- `idempotency_key`
- `provided`
- `details`
- `created`

Constraints:

- unique `(challenge_id, team_id, idempotency_key)`.

## Cache Behavior

After successful score submission, clear:

- standings cache,
- challenges cache.

This ensures scoreboard, solved status, solve counts, and user/team score values update promptly.

## Testing Plan

Planned validation:

- team user can launch challenge,
- user without team cannot launch,
- launch token expires after 5 minutes,
- launch token is single-use,
- launch verification requires admin auth,
- score submission requires admin auth,
- `0` point score creates solve,
- higher score creates delta award,
- lower/equal score creates no award,
- duplicate idempotency key does not duplicate award/event,
- all team submissions are visible to team,
- scoreboard total equals team best score,
- score submission is rejected while paused/outside CTF time/after freeze.
