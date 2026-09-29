from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "static"
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "data" / "hackathon.db")))
# Keep the no-config local run compatible with the static DOGFOOD checker
# cookies. Deployments should always provide their own secret via the environment.
SESSION_SECRET = os.getenv("SESSION_SECRET", "local-compose-secret-change-me")
SESSION_COOKIE = "hacknight_session"

app = FastAPI(
    title="Hacknight",
    description="Self-hosted hackathon submission and judging platform",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/api/static", StaticFiles(directory=STATIC_DIR), name="api-static")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt, expected = encoded.split("$", 1)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(actual, expected)


def session_value(user_id: int) -> str:
    payload = f"{user_id}.{secrets.token_urlsafe(12)}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def session_user(token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload, signature = ".".join(parts[:2]), parts[2]
    expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        user_id = int(parts[0])
    except ValueError:
        return None
    with db() as connection:
        return connection.execute(
            "SELECT id, email, name, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def public_user(user: sqlite3.Row | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {key: user[key] for key in ("id", "email", "name", "role")}


def current_user(session: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> sqlite3.Row:
    user = session_user(session)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def role_required(*roles: str):
    def dependency(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="This role cannot perform that action")
        return user

    return dependency


def audit(connection: sqlite3.Connection, user_id: int | None, action: str, entity: str, entity_id: str, metadata: dict[str, Any] | None = None) -> None:
    connection.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, action, entity, entity_id, json.dumps(metadata or {}), now()),
    )


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def rows_dict(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def optional_current_user(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> sqlite3.Row | None:
    return session_user(session)


def fixture_path() -> Path | None:
    configured = os.getenv("FIXTURES_PATH")
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            ROOT / "fixtures.json",
            ROOT / "attached_assets" / "fixtures_1790507690656.json",
        ]
    )
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return None


def fixture_name(email: str) -> str:
    local = email.split("@", 1)[0].replace("_", " ").replace(".", " ")
    return " ".join(part.capitalize() for part in local.split())


def check_rate_limit(
    connection: sqlite3.Connection,
    bucket_key: str,
    action: str,
    limit: int,
    window_seconds: int,
) -> None:
    window_start = int(time.time()) // window_seconds
    connection.execute(
        """INSERT INTO rate_limits (bucket_key, action, window_start, request_count)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(bucket_key, action, window_start)
           DO UPDATE SET request_count = request_count + 1""",
        (bucket_key, action, window_start),
    )
    count = connection.execute(
        "SELECT request_count FROM rate_limits WHERE bucket_key = ? AND action = ? AND window_start = ?",
        (bucket_key, action, window_start),
    ).fetchone()[0]
    if count > limit:
        raise HTTPException(status_code=429, detail=f"Too many {action} requests; try again shortly")
    connection.execute(
        "DELETE FROM rate_limits WHERE window_start < ?",
        (window_start - 10,),
    )


def parse_optional_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_time(value: str, label: str) -> str:
    try:
        parsed = parse_optional_time(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be a valid ISO date and time") from exc
    if not parsed:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    return parsed.isoformat()


def normalize_optional_time(value: str | None, label: str) -> str | None:
    if not value:
        return None
    try:
        parsed = parse_optional_time(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be a valid ISO date and time") from exc
    return parsed.isoformat() if parsed else None


def validate_event_timeline(
    starts_at: str,
    deadline: str,
    registration_opens_at: str | None = None,
    registration_closes_at: str | None = None,
    hackathon_ends_at: str | None = None,
    judging_opens_at: str | None = None,
    judging_closes_at: str | None = None,
    results_publish_at: str | None = None,
) -> None:
    """Reject contradictory lifecycle dates before persisting an event."""
    values = {
        "start": parse_optional_time(starts_at),
        "deadline": parse_optional_time(deadline),
        "registration start": parse_optional_time(registration_opens_at),
        "registration end": parse_optional_time(registration_closes_at),
        "hackathon end": parse_optional_time(hackathon_ends_at),
        "judging start": parse_optional_time(judging_opens_at),
        "judging end": parse_optional_time(judging_closes_at),
        "results publication": parse_optional_time(results_publish_at),
    }
    if values["deadline"] <= values["start"]:
        raise HTTPException(status_code=400, detail="Event deadline must be after the start")
    if values["registration start"] and values["registration start"] > values["start"]:
        raise HTTPException(status_code=400, detail="Registration must open before the event starts")
    if values["registration start"] and values["registration end"] and values["registration end"] < values["registration start"]:
        raise HTTPException(status_code=400, detail="Registration end must be after registration start")
    if values["hackathon end"] and values["hackathon end"] <= values["start"]:
        raise HTTPException(status_code=400, detail="Hackathon end must be after the start")
    if values["hackathon end"] and values["hackathon end"] < values["deadline"]:
        raise HTTPException(status_code=400, detail="Hackathon end must be after the submission deadline")
    if values["judging start"] and values["judging start"] < values["deadline"]:
        raise HTTPException(status_code=400, detail="Judging must start after the submission deadline")
    if values["judging start"] and values["judging end"] and values["judging end"] < values["judging start"]:
        raise HTTPException(status_code=400, detail="Judging end must be after judging start")
    if values["results publication"] and values["judging end"] and values["results publication"] < values["judging end"]:
        raise HTTPException(status_code=400, detail="Results publication must be after judging closes")


def deadline_passed(value: str) -> bool:
    try:
        deadline = parse_optional_time(value)
    except ValueError:
        return True
    return bool(deadline and deadline <= datetime.now(timezone.utc))


def registration_closed(event: sqlite3.Row | dict[str, Any]) -> bool:
    """Return whether new teams may no longer be formed for an event."""
    status = event["status"] if isinstance(event, sqlite3.Row) else event.get("status")
    if status in {"archived"}:
        return True
    registration_end = event["registration_closes_at"] if isinstance(event, sqlite3.Row) else event.get("registration_closes_at")
    return bool(registration_end and deadline_passed(registration_end))


def submissions_closed(event: sqlite3.Row | dict[str, Any]) -> bool:
    deadline = event["deadline"] if isinstance(event, sqlite3.Row) else event.get("deadline")
    return deadline_passed(str(deadline or ""))


def event_phase(event: sqlite3.Row | dict[str, Any]) -> str:
    """Return the externally visible lifecycle phase for an event."""
    def value(key: str, default: Any = None) -> Any:
        if isinstance(event, sqlite3.Row):
            try:
                return event[key]
            except (IndexError, KeyError):
                return default
        return event.get(key, default)

    if value("status") == "archived":
        return "archived"
    if value("results_published"):
        return "results_published"
    current = datetime.now(timezone.utc)
    registration_start = parse_optional_time(value("registration_opens_at"))
    registration_end = parse_optional_time(value("registration_closes_at"))
    judging_start = parse_optional_time(value("judging_opens_at"))
    judging_end = parse_optional_time(value("judging_closes_at"))
    hackathon_start = parse_optional_time(value("starts_at"))
    hackathon_end = parse_optional_time(value("hackathon_ends_at"))
    submission_deadline = parse_optional_time(value("deadline"))
    if value("status") == "draft":
        return "draft"
    if registration_start and current < registration_start:
        return "draft"
    if registration_end and current > registration_end and hackathon_start and current < hackathon_start:
        return "registration_closed"
    if hackathon_start and current < hackathon_start:
        return "registration_open"
    if submission_deadline and current <= submission_deadline:
        return "hackathon_active"
    if judging_start and current >= judging_start and (not judging_end or current <= judging_end):
        return "judging_active"
    if judging_end and current > judging_end:
        return "results_pending"
    if hackathon_end and current > hackathon_end:
        return "submission_closed"
    return "submission_closed"


def decode_json_field(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def public_project(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    item = dict(row)
    item["image_urls"] = decode_json_field(item.get("image_urls"), [])
    item["tech_tags"] = decode_json_field(item.get("tech_tags"), [])
    item["custom_answers"] = decode_json_field(item.get("custom_answers"), {})
    return item


def validate_custom_answers(
    connection: sqlite3.Connection,
    event_id: int,
    answers: dict[str, Any],
) -> None:
    questions = connection.execute(
        "SELECT question_key, required FROM custom_questions WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    allowed = {question["question_key"] for question in questions}
    unknown = set(answers) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown custom question: {sorted(unknown)[0]}")
    missing = [
        question["question_key"]
        for question in questions
        if question["required"] and answers.get(question["question_key"]) in (None, "")
    ]
    if missing:
        raise HTTPException(status_code=400, detail=f"Required custom question is missing: {missing[0]}")


def voting_policy(
    event: sqlite3.Row,
    request: Request,
    user: sqlite3.Row | None,
) -> dict[str, Any]:
    access = event["voting_access"] or "authenticated"
    mode = event["voting_mode"] or "one_per_project"
    if access not in {"open", "email", "authenticated"}:
        access = "authenticated"
    if mode not in {"one_per_project", "one_per_event", "quadratic"}:
        mode = "one_per_project"
    email_header = request.headers.get("x-voter-email", "").strip().lower()
    voter_key = f"user:{user['id']}" if user else ""
    if access == "open" and not voter_key:
        voter_key = request.headers.get("x-voter-key", "").strip()
        if not voter_key and request.client:
            voter_key = f"ip:{request.client.host}"
    if access == "email" and not voter_key:
        if email_header and "@" in email_header:
            voter_key = f"email:{email_header}"
        else:
            voter_key = ""
    opens_at = parse_optional_time(event["voting_opens_at"])
    closes_at = parse_optional_time(event["voting_closes_at"])
    current = datetime.now(timezone.utc)
    window_open = not opens_at or current >= opens_at
    window_closed = bool(closes_at and current > closes_at)
    access_allowed = bool(voter_key) and (access != "authenticated" or user is not None)
    if access == "email":
        access_allowed = bool(voter_key) and (user is not None or bool(email_header))
    organizer_override = bool(user is not None and user["role"] in {"organizer", "admin"})
    has_voting_window = bool(opens_at or closes_at)
    return {
        "access": access,
        "mode": mode,
        "voter_key": voter_key,
        "window_open": window_open,
        "window_closed": window_closed,
        "can_vote": access_allowed and window_open and not window_closed,
        "results_revealed": organizer_override or window_closed or (bool(event["results_visible"]) and not has_voting_window),
        "opens_at": event["voting_opens_at"],
        "closes_at": event["voting_closes_at"],
        "quadratic_budget": int(event["quadratic_budget"] or 16),
    }


def require_voting_window(policy: dict[str, Any]) -> None:
    if not policy["can_vote"]:
        if not policy["window_open"]:
            raise HTTPException(status_code=403, detail="Voting has not opened yet")
        if policy["window_closed"]:
            raise HTTPException(status_code=403, detail="Voting has closed")
        raise HTTPException(status_code=401, detail="This ballot requires the configured voter access")


def dispatch_webhook(connection: sqlite3.Connection, event_type: str, payload: dict[str, Any]) -> None:
    """Record and best-effort deliver local webhook subscriptions.

    A webhook is never required for the core app to work. Delivery is bounded by
    a short timeout and failures stay visible in the delivery log for retries or
    operator inspection.
    """
    event_id = payload.get("event_id")
    hooks = connection.execute(
        """SELECT * FROM webhooks
           WHERE active = 1 AND (event_id IS NULL OR event_id = ?)
           ORDER BY id""",
        (event_id,),
    ).fetchall()
    body = json.dumps({"event": event_type, "created_at": now(), "data": payload}).encode()
    for hook in hooks:
        configured_events = {item.strip() for item in (hook["events"] or "*").split(",") if item.strip()}
        if "*" not in configured_events and event_type not in configured_events:
            continue
        delivery = connection.execute(
            """INSERT INTO webhook_deliveries
               (webhook_id, event_type, payload, status, attempt_count, created_at)
               VALUES (?, ?, ?, 'pending', 0, ?)""",
            (hook["id"], event_type, body.decode(), now()),
        )
        signature = hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            hook["url"],
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Hacknight-Webhook/1.0",
                "X-Hacknight-Event": event_type,
                "X-Hacknight-Signature": f"sha256={signature}",
            },
        )
        status = "delivered"
        response_code: int | None = None
        error = ""
        delivered_at: str | None = now()
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                response_code = response.status
                if response_code >= 400:
                    status = "failed"
                    delivered_at = None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status = "failed"
            error = str(exc)[:500]
            delivered_at = None
        connection.execute(
            """UPDATE webhook_deliveries
               SET status = ?, response_code = ?, error = ?, attempt_count = 1, delivered_at = ?
               WHERE id = ?""",
            (status, response_code, error, delivered_at, delivery.lastrowid),
        )


def seed_shared_fixture(connection: sqlite3.Connection, fixture: dict[str, Any]) -> None:
    """Load the supplied shared DOGFOOD dataset into the normal app schema."""
    organizer_cursor = connection.execute(
        "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, 'organizer', ?, ?)",
        ("organizer@hacknight.local", "Hacknight Organizer", hash_password("organizer"), now()),
    )
    organizer_id = organizer_cursor.lastrowid
    user_ids: dict[str, int] = {}
    judge_ids: dict[str, int] = {}

    for judge in fixture.get("judges", []):
        cursor = connection.execute(
            "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, 'judge', ?, ?)",
            (judge["email"], judge["name"], hash_password("judge"), now()),
        )
        user_ids[judge["email"]] = cursor.lastrowid
        judge_ids[judge["id"]] = cursor.lastrowid

    member_emails = {
        email
        for team in fixture.get("teams", [])
        for email in team.get("members", [])
    }
    for email in sorted(member_emails):
        if email in user_ids:
            continue
        cursor = connection.execute(
            "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, 'participant', ?, ?)",
            (email, fixture_name(email), hash_password("participant"), now()),
        )
        user_ids[email] = cursor.lastrowid

    event_data = fixture.get("event", {})
    close_at = normalize_time(event_data.get("submissions_close", ""), "Fixture submission close")
    event_cursor = connection.execute(
        """INSERT INTO events
           (name, slug, tagline, description, starts_at, deadline, status, created_by, created_at,
            voting_access, voting_mode, voting_opens_at, voting_closes_at, results_visible, quadratic_budget, ballot_seed)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, 'open', 'one_per_project', ?, ?, 1, 16, ?)""",
        (
            event_data.get("name", "Sample Hack 2026"),
            event_data.get("id", "evt_01"),
            "A shared DOGFOOD judging fixture.",
            "The common hackathon dataset used to verify the submission and judging portal.",
            "2026-02-01T00:00:00+00:00",
            close_at,
            organizer_id,
            now(),
            "2026-01-01T00:00:00+00:00",
            close_at,
            secrets.token_hex(12),
        ),
    )
    event_id = event_cursor.lastrowid

    palette = ["#ff3b86", "#25e0c2", "#a78bfa", "#4da3ff", "#f4c95d", "#ff815c", "#76e06f", "#d58cff"]
    track_ids: dict[str, int] = {}
    for index, track in enumerate(fixture.get("tracks", [])):
        cursor = connection.execute(
            "INSERT INTO tracks (event_id, name, color) VALUES (?, ?, ?)",
            (event_id, track["name"], palette[index % len(palette)]),
        )
        track_ids[track["id"]] = cursor.lastrowid

    for title, amount in [("Best in show", "$2,500"), ("Most useful", "$1,000"), ("People's choice", "$500")]:
        connection.execute(
            "INSERT INTO prizes (event_id, title, amount) VALUES (?, ?, ?)",
            (event_id, title, amount),
        )

    criteria = [
        ("functionality", "Functionality & completeness", "Does the project deliver what it promises?", 50),
        ("quality", "Quality & operability", "Is the solution careful, usable, and maintainable?", 30),
        ("innovation", "Innovation & clarity", "Is the idea original and clearly communicated?", 20),
    ]
    criterion_ids: dict[str, int] = {}
    for sort_order, (key, name, description, weight) in enumerate(criteria, start=1):
        cursor = connection.execute(
            """INSERT INTO criteria (event_id, name, description, weight, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, name, description, weight, sort_order),
        )
        criterion_ids[key] = cursor.lastrowid

    team_ids: dict[str, int] = {}
    for team in fixture.get("teams", []):
        cursor = connection.execute(
            "INSERT INTO teams (event_id, name, invite_code, created_at) VALUES (?, ?, ?, ?)",
            (event_id, team["name"], f"fixture-{team['id']}", now()),
        )
        team_ids[team["id"]] = cursor.lastrowid
        for index, email in enumerate(team.get("members", [])):
            connection.execute(
                """INSERT OR IGNORE INTO team_members (team_id, user_id, member_role)
                   VALUES (?, ?, ?)""",
                (cursor.lastrowid, user_ids[email], "captain" if index == 0 else "member"),
            )

    project_ids: dict[str, int] = {}
    for project in fixture.get("projects", []):
        submitted_at = normalize_time(project["submitted_at"], "Fixture submitted_at")
        slug = f"{project['id']}-{'-'.join(project['title'].lower().split())}"
        cursor = connection.execute(
            """INSERT INTO projects
               (event_id, team_id, track_id, title, slug, summary, repo_url, demo_url,
                status, submitted_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)""",
            (
                event_id,
                team_ids[project["team"]],
                track_ids[project["track"]],
                project["title"],
                slug,
                project["summary"],
                project["repo_url"],
                f"https://example.org/demo/{project['id']}",
                submitted_at,
                submitted_at,
            ),
        )
        project_ids[project["id"]] = cursor.lastrowid

    for scorecard in fixture.get("scores", []):
        assignment_cursor = connection.execute(
            """INSERT OR IGNORE INTO assignments (project_id, judge_id) VALUES (?, ?)""",
            (project_ids[scorecard["project"]], judge_ids[scorecard["judge"]]),
        )
        assignment = connection.execute(
            "SELECT id FROM assignments WHERE project_id = ? AND judge_id = ?",
            (project_ids[scorecard["project"]], judge_ids[scorecard["judge"]]),
        ).fetchone()
        if not assignment:
            raise RuntimeError(f"Unable to seed assignment for {scorecard['judge']}")
        for criterion_key, score in scorecard.get("criteria", {}).items():
            criterion_id = criterion_ids.get(criterion_key)
            if criterion_id is None:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO scores
                   (assignment_id, criterion_id, score, note, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (assignment["id"], criterion_id, score, scorecard.get("comment", ""), now()),
            )


def init_db() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              email TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('visitor','participant','judge','organizer','admin')),
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              slug TEXT NOT NULL UNIQUE,
              tagline TEXT NOT NULL,
              description TEXT NOT NULL,
              starts_at TEXT NOT NULL,
              deadline TEXT NOT NULL,
              registration_opens_at TEXT,
              registration_closes_at TEXT,
              hackathon_ends_at TEXT,
              judging_opens_at TEXT,
              judging_closes_at TEXT,
              results_publish_at TEXT,
              status TEXT NOT NULL DEFAULT 'active',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              voting_access TEXT NOT NULL DEFAULT 'authenticated',
              voting_mode TEXT NOT NULL DEFAULT 'one_per_project',
              voting_opens_at TEXT,
              voting_closes_at TEXT,
              results_visible INTEGER NOT NULL DEFAULT 0,
              results_published INTEGER NOT NULL DEFAULT 0,
              feedback_visible INTEGER NOT NULL DEFAULT 0,
              quadratic_budget INTEGER NOT NULL DEFAULT 16,
              ballot_seed TEXT NOT NULL DEFAULT '',
              team_min_size INTEGER NOT NULL DEFAULT 1,
              team_max_size INTEGER NOT NULL DEFAULT 4,
              comments_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tracks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              color TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prizes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              amount TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS result_awards (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              prize_id INTEGER REFERENCES prizes(id) ON DELETE SET NULL,
              project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
              title TEXT NOT NULL,
              recipient_name TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(event_id, prize_id),
              UNIQUE(event_id, title)
            );
            CREATE TABLE IF NOT EXISTS teams (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              invite_code TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS team_members (
              team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              member_role TEXT NOT NULL DEFAULT 'member',
              PRIMARY KEY(team_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS projects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              team_id INTEGER NOT NULL REFERENCES teams(id),
              track_id INTEGER NOT NULL REFERENCES tracks(id),
              title TEXT NOT NULL,
              slug TEXT NOT NULL UNIQUE,
              summary TEXT NOT NULL,
              tagline TEXT NOT NULL DEFAULT '',
              long_description TEXT NOT NULL DEFAULT '',
              thumbnail_url TEXT NOT NULL DEFAULT '',
              image_urls TEXT NOT NULL DEFAULT '[]',
              demo_video_url TEXT NOT NULL DEFAULT '',
              tech_tags TEXT NOT NULL DEFAULT '[]',
              custom_answers TEXT NOT NULL DEFAULT '{}',
              repo_url TEXT NOT NULL,
              demo_url TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft',
              submitted_at TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS criteria (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              description TEXT NOT NULL,
              weight INTEGER NOT NULL,
              sort_order INTEGER NOT NULL,
              minimum_score INTEGER NOT NULL DEFAULT 1,
              maximum_score INTEGER NOT NULL DEFAULT 5
            );
            CREATE TABLE IF NOT EXISTS assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              judge_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              UNIQUE(project_id, judge_id)
            );
            CREATE TABLE IF NOT EXISTS pairwise_comparisons (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              judge_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_a_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              project_b_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              winner_project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              UNIQUE(event_id, judge_id, project_a_id, project_b_id)
            );
            CREATE TABLE IF NOT EXISTS scores (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
              criterion_id INTEGER NOT NULL REFERENCES criteria(id) ON DELETE CASCADE,
              score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
              note TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              UNIQUE(assignment_id, criterion_id)
            );
            CREATE TABLE IF NOT EXISTS votes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
              event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
              voter_key TEXT NOT NULL,
              quantity INTEGER NOT NULL DEFAULT 1,
              credits_spent INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              UNIQUE(project_id, voter_key)
            );
            CREATE TABLE IF NOT EXISTS comments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
              author_name TEXT NOT NULL,
              body TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              report_reason TEXT NOT NULL DEFAULT '',
              reported_at TEXT,
              moderated_by INTEGER REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS custom_questions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              question_key TEXT NOT NULL,
              prompt TEXT NOT NULL,
              question_type TEXT NOT NULL DEFAULT 'text',
              required INTEGER NOT NULL DEFAULT 0,
              options TEXT NOT NULL DEFAULT '[]',
              sort_order INTEGER NOT NULL DEFAULT 1,
              UNIQUE(event_id, question_key)
            );
            CREATE TABLE IF NOT EXISTS judge_tracks (
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              judge_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
              PRIMARY KEY(event_id, judge_id, track_id)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER REFERENCES users(id),
              action TEXT NOT NULL,
              entity TEXT NOT NULL,
              entity_id TEXT NOT NULL,
              metadata TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS judge_invitations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              email TEXT NOT NULL,
              invited_by INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'pending',
              token TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
              bucket_key TEXT NOT NULL,
              action TEXT NOT NULL,
              window_start INTEGER NOT NULL,
              request_count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(bucket_key, action, window_start)
            );
            CREATE TABLE IF NOT EXISTS webhooks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
              url TEXT NOT NULL,
              secret TEXT NOT NULL,
              events TEXT NOT NULL DEFAULT '*',
              active INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
              event_type TEXT NOT NULL,
              payload TEXT NOT NULL,
              status TEXT NOT NULL,
              response_code INTEGER,
              error TEXT NOT NULL DEFAULT '',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              delivered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS certificates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              recipient_name TEXT NOT NULL,
              award_title TEXT NOT NULL,
              certificate_code TEXT NOT NULL UNIQUE,
              issued_by INTEGER NOT NULL REFERENCES users(id),
              issued_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS judge_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
              judge_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              record_code TEXT NOT NULL UNIQUE,
              payload TEXT NOT NULL,
              signature TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()}
        event_migrations = {
            "registration_opens_at": "TEXT",
            "registration_closes_at": "TEXT",
            "hackathon_ends_at": "TEXT",
            "judging_opens_at": "TEXT",
            "judging_closes_at": "TEXT",
            "results_publish_at": "TEXT",
            "voting_access": "TEXT NOT NULL DEFAULT 'authenticated'",
            "voting_mode": "TEXT NOT NULL DEFAULT 'one_per_project'",
            "voting_opens_at": "TEXT",
            "voting_closes_at": "TEXT",
            "results_visible": "INTEGER NOT NULL DEFAULT 0",
            "results_published": "INTEGER NOT NULL DEFAULT 0",
            "feedback_visible": "INTEGER NOT NULL DEFAULT 0",
            "quadratic_budget": "INTEGER NOT NULL DEFAULT 16",
            "ballot_seed": "TEXT NOT NULL DEFAULT ''",
            "team_min_size": "INTEGER NOT NULL DEFAULT 1",
            "team_max_size": "INTEGER NOT NULL DEFAULT 4",
            "comments_enabled": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, declaration in event_migrations.items():
            if column not in event_columns:
                connection.execute(f"ALTER TABLE events ADD COLUMN {column} {declaration}")
        project_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)").fetchall()}
        project_migrations = {
            "tagline": "TEXT NOT NULL DEFAULT ''",
            "long_description": "TEXT NOT NULL DEFAULT ''",
            "thumbnail_url": "TEXT NOT NULL DEFAULT ''",
            "image_urls": "TEXT NOT NULL DEFAULT '[]'",
            "demo_video_url": "TEXT NOT NULL DEFAULT ''",
            "tech_tags": "TEXT NOT NULL DEFAULT '[]'",
            "custom_answers": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, declaration in project_migrations.items():
            if column not in project_columns:
                connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {declaration}")
        criterion_columns = {row["name"] for row in connection.execute("PRAGMA table_info(criteria)").fetchall()}
        criterion_migrations = {
            "minimum_score": "INTEGER NOT NULL DEFAULT 1",
            "maximum_score": "INTEGER NOT NULL DEFAULT 5",
        }
        for column, declaration in criterion_migrations.items():
            if column not in criterion_columns:
                connection.execute(f"ALTER TABLE criteria ADD COLUMN {column} {declaration}")
        comment_columns = {row["name"] for row in connection.execute("PRAGMA table_info(comments)").fetchall()}
        comment_migrations = {
            "author_user_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "report_reason": "TEXT NOT NULL DEFAULT ''",
            "reported_at": "TEXT",
            "moderated_by": "INTEGER REFERENCES users(id)",
        }
        for column, declaration in comment_migrations.items():
            if column not in comment_columns:
                connection.execute(f"ALTER TABLE comments ADD COLUMN {column} {declaration}")
        vote_columns = {row["name"] for row in connection.execute("PRAGMA table_info(votes)").fetchall()}
        vote_migrations = {
            "user_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
            "event_id": "INTEGER REFERENCES events(id) ON DELETE CASCADE",
            "quantity": "INTEGER NOT NULL DEFAULT 1",
            "credits_spent": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, declaration in vote_migrations.items():
            if column not in vote_columns:
                connection.execute(f"ALTER TABLE votes ADD COLUMN {column} {declaration}")
        connection.execute("UPDATE votes SET event_id = (SELECT event_id FROM projects WHERE projects.id = votes.project_id) WHERE event_id IS NULL")
        connection.execute("UPDATE events SET voting_access = 'open', results_visible = 1 WHERE ballot_seed = '' OR ballot_seed IS NULL")
        connection.execute("UPDATE events SET ballot_seed = lower(hex(randomblob(8))) WHERE ballot_seed = '' OR ballot_seed IS NULL")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_votes_user ON votes(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_votes_event_voter ON votes(event_id, voter_key)")
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return

        shared_fixture = fixture_path()
        if shared_fixture:
            with shared_fixture.open(encoding="utf-8") as fixture_file:
                seed_shared_fixture(connection, json.load(fixture_file))
            return

        users = [
            ("organizer@hacknight.local", "Maya Chen", "organizer", "organizer"),
            ("judge@hacknight.local", "Ravi Shah", "judge", "judge"),
            ("judge2@hacknight.local", "Noor Ali", "judge", "judge"),
            ("builder@hacknight.local", "Alex Kim", "participant", "builder"),
        ]
        user_ids: dict[str, int] = {}
        for email, name, role, password in users:
            cursor = connection.execute(
                "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (email, name, role, hash_password(password), now()),
            )
            user_ids[role + name] = cursor.lastrowid
        organizer_id = user_ids["organizerMaya Chen"]
        judge_id = user_ids["judgeRavi Shah"]
        judge2_id = user_ids["judgeNoor Ali"]
        participant_id = user_ids["participantAlex Kim"]

        event_cursor = connection.execute(
            """INSERT INTO events (name, slug, tagline, description, starts_at, deadline, status, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                "Hacknight Zero",
                "hacknight-zero",
                "Build the tools that make the next hackathon better.",
                "A self-hosted, API-first build sprint for systems that help people ship together.",
                "2026-10-02T18:00:00+00:00",
                "2026-10-04T18:00:00+00:00",
                organizer_id,
                now(),
            ),
        )
        event_id = event_cursor.lastrowid
        track_ids: list[int] = []
        for name, color in [("Open infrastructure", "#ff3b86"), ("Human systems", "#25e0c2"), ("Wildcard", "#a78bfa")]:
            cursor = connection.execute(
                "INSERT INTO tracks (event_id, name, color) VALUES (?, ?, ?)",
                (event_id, name, color),
            )
            track_ids.append(cursor.lastrowid)
        for title, amount in [("Best in show", "$2,500"), ("Most useful", "$1,000"), ("People's choice", "$500")]:
            connection.execute("INSERT INTO prizes (event_id, title, amount) VALUES (?, ?, ?)", (event_id, title, amount))
        for name, description, weight, order in [
            ("Tier completion & correctness", "Does the project deliver what it promises, with the important paths working end to end?", 40, 1),
            ("Judging integrity", "Is the solution defensible, tested, and careful about edge cases and access control?", 25, 2),
            ("Adoptability & operability", "Could another organizer run this locally and understand how to use it?", 20, 3),
            ("Code quality & innovation", "Is the implementation thoughtful, maintainable, and meaningfully original?", 15, 4),
        ]:
            connection.execute(
                "INSERT INTO criteria (event_id, name, description, weight, sort_order) VALUES (?, ?, ?, ?, ?)",
                (event_id, name, description, weight, order),
            )

        teams = [
            ("Night Shift", participant_id),
            ("Kernel Panic", None),
            ("Civic Stack", None),
            ("Good Problems", None),
        ]
        team_ids: list[int] = []
        for team_name, member_id in teams:
            cursor = connection.execute(
                "INSERT INTO teams (event_id, name, invite_code, created_at) VALUES (?, ?, ?, ?)",
                (event_id, team_name, secrets.token_urlsafe(8), now()),
            )
            team_ids.append(cursor.lastrowid)
            if member_id:
                connection.execute(
                    "INSERT INTO team_members (team_id, user_id, member_role) VALUES (?, ?, 'captain')",
                    (cursor.lastrowid, member_id),
                )
        sample_projects = [
            (team_ids[0], track_ids[0], "Signal Garden", "A calmer way to see what needs attention in a noisy community.", "https://github.com/hacknight/signal-garden", "https://signal-garden.local", "submitted"),
            (team_ids[1], track_ids[1], "Open Hours", "A simple, local-first directory for community spaces and their real availability.", "https://github.com/hacknight/open-hours", "https://open-hours.local", "submitted"),
            (team_ids[2], track_ids[0], "Patch Notes", "Turn messy project changes into a changelog people can actually read.", "https://github.com/hacknight/patch-notes", "https://patch-notes.local", "submitted"),
            (team_ids[3], track_ids[2], "Tiny Table", "A tiny offline data room for teams that do not need a warehouse.", "https://github.com/hacknight/tiny-table", "https://tiny-table.local", "draft"),
        ]
        project_ids: list[int] = []
        for team_id, track_id, title, summary, repo, demo, status in sample_projects:
            cursor = connection.execute(
                """INSERT INTO projects (event_id, team_id, track_id, title, slug, summary, repo_url, demo_url, status, submitted_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, team_id, track_id, title, title.lower().replace(" ", "-"), summary, repo, demo, status, now() if status == "submitted" else None, now()),
            )
            project_ids.append(cursor.lastrowid)
        for project_id in [project_ids[0], project_ids[1]]:
            connection.execute("INSERT INTO assignments (project_id, judge_id) VALUES (?, ?)", (project_id, judge_id))
        for project_id in [project_ids[1], project_ids[2]]:
            connection.execute("INSERT INTO assignments (project_id, judge_id) VALUES (?, ?)", (project_id, judge2_id))
        connection.execute(
            "INSERT INTO comments (project_id, author_name, body, created_at) VALUES (?, ?, ?, ?)",
            (project_ids[0], "Community member", "The focus mode is a great detail. Would love to try this with a real neighborhood group.", now()),
        )


class LoginBody(BaseModel):
    email: str
    password: str


class RegisterBody(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=8, max_length=200)


class EventBody(BaseModel):
    name: str = Field(min_length=2)
    tagline: str = Field(min_length=2)
    description: str = Field(min_length=2)
    starts_at: str
    deadline: str
    registration_opens_at: str | None = None
    registration_closes_at: str | None = None
    hackathon_ends_at: str | None = None
    judging_opens_at: str | None = None
    judging_closes_at: str | None = None
    results_publish_at: str | None = None
    status: str = "active"
    team_min_size: int = Field(default=1, ge=1, le=4)
    team_max_size: int = Field(default=4, ge=1, le=4)
    comments_enabled: bool = True
    tracks: list[dict[str, str]] = []
    prizes: list[dict[str, str]] = []
    voting_access: str = "authenticated"
    voting_mode: str = "one_per_project"
    voting_opens_at: str | None = None
    voting_closes_at: str | None = None
    results_visible: bool = False
    feedback_visible: bool = False
    quadratic_budget: int = Field(default=16, ge=1, le=1000)
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    custom_questions: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class VotingPolicyBody(BaseModel):
    voting_access: str = "authenticated"
    voting_mode: str = "one_per_event"
    voting_opens_at: str | None = None
    voting_closes_at: str | None = None
    results_visible: bool = False
    quadratic_budget: int = Field(default=16, ge=1, le=1000)


class CriterionInput(BaseModel):
    id: int | None = None
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=2, max_length=500)
    weight: int = Field(ge=1, le=100)
    sort_order: int = Field(default=1, ge=1, le=100)
    minimum_score: int = Field(default=1, ge=1, le=5)
    maximum_score: int = Field(default=5, ge=1, le=5)


class CriteriaUpdateBody(BaseModel):
    criteria: list[CriterionInput] = Field(min_length=1, max_length=20)


class ProjectBody(BaseModel):
    event_id: int
    team_id: int
    track_id: int
    title: str = Field(min_length=2)
    summary: str = Field(min_length=10)
    tagline: str = ""
    long_description: str = ""
    thumbnail_url: str = ""
    image_urls: list[str] = Field(default_factory=list, max_length=20)
    demo_video_url: str = ""
    tech_tags: list[str] = Field(default_factory=list, max_length=30)
    custom_answers: dict[str, Any] = Field(default_factory=dict)
    repo_url: str
    demo_url: str
    status: str = "draft"


class ScoreBody(BaseModel):
    scores: dict[str, int]
    note: str = ""


class VoteBody(BaseModel):
    project_id: int
    quantity: int = Field(default=1, ge=1, le=100)


class CommentBody(BaseModel):
    project_id: int
    body: str = Field(min_length=2, max_length=500)


class TeamBody(BaseModel):
    event_id: int
    name: str = Field(min_length=2, max_length=80)


class TeamJoinBody(BaseModel):
    invite_code: str = Field(min_length=4)


class TeamInviteBody(BaseModel):
    email: str | None = None


class AutoAssignmentBody(BaseModel):
    event_id: int
    reviews_per_project: int = Field(default=3, ge=1, le=10)


class CustomQuestionBody(BaseModel):
    event_id: int
    questions: list[dict[str, Any]] = Field(max_length=20)


class CommentReportBody(BaseModel):
    reason: str = Field(min_length=2, max_length=300)


class CommentModerationBody(BaseModel):
    status: str = Field(pattern="^(active|hidden|removed)$")


class ResultsPublishBody(BaseModel):
    event_id: int


class FeedbackPolicyBody(BaseModel):
    feedback_visible: bool


class AwardBody(BaseModel):
    event_id: int
    project_id: int | None = None
    prize_id: int | None = None
    title: str = Field(min_length=2, max_length=160)
    recipient_name: str = Field(default="", max_length=160)


class JudgeInvitationBody(BaseModel):
    event_id: int
    email: str


class AssignmentBody(BaseModel):
    judge_id: int
    project_ids: list[int] = Field(min_length=1)


class JudgeTrackBody(BaseModel):
    event_id: int
    judge_id: int
    track_ids: list[int] = Field(default_factory=list, max_length=100)


class PairwiseComparisonBody(BaseModel):
    event_id: int
    project_a_id: int
    project_b_id: int
    winner_project_id: int


class ImportProjectsBody(BaseModel):
    event_id: int
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class ImportRowsBody(BaseModel):
    event_id: int
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class ImportEventBody(BaseModel):
    event_id: int
    payload: dict[str, Any]


class WebhookBody(BaseModel):
    event_id: int | None = None
    url: str = Field(min_length=8, max_length=500)
    events: list[str] = Field(default_factory=lambda: ["*"])
    secret: str = Field(default="", max_length=200)


class CertificateBody(BaseModel):
    project_id: int
    recipient_name: str = Field(min_length=2, max_length=120)
    award_title: str = Field(min_length=2, max_length=120)


class JudgeRecordBody(BaseModel):
    event_id: int
    judge_id: int


class UserRoleBody(BaseModel):
    role: str = Field(pattern="^(participant|judge|organizer|admin)$")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/", response_class=HTMLResponse)
def api_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/session")
def session(user: sqlite3.Row | None = Depends(lambda session=Cookie(default=None, alias=SESSION_COOKIE): session_user(session))) -> dict[str, Any]:
    return {"user": public_user(user)}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    bucket = f"ip:{request.client.host if request.client else 'unknown'}"
    with db() as connection:
        check_rate_limit(connection, bucket, "login", 20, 600)
        user = connection.execute("SELECT * FROM users WHERE lower(email) = lower(?)", (body.email.strip(),)).fetchone()
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Email or password is incorrect")
        audit(connection, user["id"], "login", "user", str(user["id"]))
    response.set_cookie(
        SESSION_COOKIE,
        session_value(user["id"]),
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
        max_age=60 * 60 * 12,
    )
    return {"user": public_user(user)}


@app.post("/api/auth/register")
def register(body: RegisterBody, request: Request, response: Response) -> dict[str, Any]:
    name = body.name.strip()
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    bucket = f"ip:{request.client.host if request.client else 'unknown'}"
    with db() as connection:
        check_rate_limit(connection, bucket, "register", 10, 3600)
        if connection.execute("SELECT 1 FROM users WHERE lower(email) = ?", (email,)).fetchone():
            raise HTTPException(status_code=409, detail="An account with this email already exists")
        cursor = connection.execute(
            """INSERT INTO users (email, name, role, password_hash, created_at)
               VALUES (?, ?, 'participant', ?, ?)""",
            (email, name, hash_password(body.password), now()),
        )
        user = connection.execute("SELECT id, email, name, role FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        audit(connection, user["id"], "register", "user", str(user["id"]))
    response.set_cookie(
        SESSION_COOKIE,
        session_value(user["id"]),
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
        max_age=60 * 60 * 12,
    )
    return {"user": public_user(user)}


@app.post("/api/auth/logout")
def logout(response: Response, user: sqlite3.Row = Depends(current_user)) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/events")
def events() -> dict[str, Any]:
    with db() as connection:
        items = rows_dict(connection.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall())
        for item in items:
            item["phase"] = event_phase(item)
            item["tracks"] = rows_dict(connection.execute("SELECT id, name, color FROM tracks WHERE event_id = ?", (item["id"],)).fetchall())
            item["prizes"] = rows_dict(connection.execute("SELECT id, title, amount FROM prizes WHERE event_id = ?", (item["id"],)).fetchall())
            item["custom_questions"] = rows_dict(connection.execute(
                """SELECT id, question_key, prompt, question_type, required, options, sort_order
                   FROM custom_questions WHERE event_id = ? ORDER BY sort_order, id""",
                (item["id"],),
            ).fetchall())
            for question in item["custom_questions"]:
                question["options"] = decode_json_field(question["options"], [])
    return {"items": items}


@app.get("/api/events/{event_id}")
def event_detail(event_id: int) -> dict[str, Any]:
    payload = events()
    item = next((item for item in payload["items"] if item["id"] == event_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"event": item}


@app.post("/api/events")
def create_event(body: EventBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    if body.voting_access not in {"open", "email", "authenticated"}:
        raise HTTPException(status_code=400, detail="Voting access must be open, email, or authenticated")
    if body.voting_mode not in {"one_per_project", "one_per_event", "quadratic"}:
        raise HTTPException(status_code=400, detail="Voting mode is invalid")
    if body.status not in {"draft", "active", "archived"}:
        raise HTTPException(status_code=400, detail="Event status is invalid")
    if body.team_min_size > body.team_max_size:
        raise HTTPException(status_code=400, detail="Minimum team size cannot exceed maximum team size")
    starts_at = normalize_time(body.starts_at, "Event start")
    deadline = normalize_time(body.deadline, "Event deadline")
    if parse_optional_time(deadline) <= parse_optional_time(starts_at):
        raise HTTPException(status_code=400, detail="Event deadline must be after the start")
    registration_opens_at = normalize_optional_time(body.registration_opens_at, "Registration start")
    registration_closes_at = normalize_optional_time(body.registration_closes_at, "Registration end")
    hackathon_ends_at = normalize_optional_time(body.hackathon_ends_at, "Hackathon end")
    judging_opens_at = normalize_optional_time(body.judging_opens_at, "Judging start")
    judging_closes_at = normalize_optional_time(body.judging_closes_at, "Judging end")
    results_publish_at = normalize_optional_time(body.results_publish_at, "Results publication date")
    opens_at = normalize_optional_time(body.voting_opens_at, "Voting start")
    closes_at = normalize_optional_time(body.voting_closes_at, "Voting end")
    validate_event_timeline(
        starts_at, deadline, registration_opens_at, registration_closes_at,
        hackathon_ends_at, judging_opens_at, judging_closes_at, results_publish_at,
    )
    if opens_at and closes_at and closes_at <= opens_at:
        raise HTTPException(status_code=400, detail="Voting close must be after voting open")
    slug = "-".join(body.name.lower().split())
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO events
               (name, slug, tagline, description, starts_at, deadline, registration_opens_at,
                registration_closes_at, hackathon_ends_at, judging_opens_at, judging_closes_at,
                results_publish_at, status, created_by, created_at, voting_access, voting_mode,
                 voting_opens_at, voting_closes_at, results_visible, results_published, feedback_visible,
                quadratic_budget, ballot_seed, team_min_size, team_max_size, comments_enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                body.name.strip(), f"{slug}-{secrets.token_hex(2)}", body.tagline.strip(), body.description.strip(),
                starts_at, deadline, registration_opens_at, registration_closes_at, hackathon_ends_at,
                judging_opens_at, judging_closes_at, results_publish_at, body.status, user["id"], now(),
                 body.voting_access, body.voting_mode, opens_at, closes_at, int(body.results_visible), 0,
                 int(body.feedback_visible), body.quadratic_budget, secrets.token_hex(12), body.team_min_size, body.team_max_size,
                int(body.comments_enabled),
            ),
        )
        event_id = cursor.lastrowid
        for index, track in enumerate(body.tracks or [{"name": "General", "color": "#ff3b86"}]):
            connection.execute("INSERT INTO tracks (event_id, name, color) VALUES (?, ?, ?)", (event_id, track.get("name", "Track"), track.get("color", ["#ff3b86", "#25e0c2", "#a78bfa"][index % 3])))
        for prize in body.prizes:
            connection.execute("INSERT INTO prizes (event_id, title, amount) VALUES (?, ?, ?)", (event_id, prize.get("title", "Prize"), prize.get("amount", "")))
        criteria = body.criteria or [
            {
                "name": "Tier Completion & Correctness",
                "description": "Does the project deliver what it promises, end to end?",
                "weight": 40,
                "sort_order": 1,
            },
            {
                "name": "Judging Integrity",
                "description": "Is the solution defensible, tested, and careful about access control?",
                "weight": 25,
                "sort_order": 2,
            },
            {
                "name": "Adoptability & Operability",
                "description": "Could another organizer run it locally and understand how to use it?",
                "weight": 20,
                "sort_order": 3,
            },
            {
                "name": "Code Quality & Innovation",
                "description": "Is the implementation thoughtful, maintainable, and original?",
                "weight": 15,
                "sort_order": 4,
            },
        ]
        if sum(int(item.get("weight", 0)) for item in criteria) != 100:
            raise HTTPException(status_code=400, detail="Rubric weights must add up to 100")
        for index, criterion in enumerate(criteria, start=1):
            name = str(criterion.get("name", "")).strip()
            description = str(criterion.get("description", "")).strip()
            weight = int(criterion.get("weight", 0))
            if len(name) < 2 or len(description) < 2 or not 1 <= weight <= 100:
                raise HTTPException(status_code=400, detail="Each rubric criterion needs a name, description, and valid weight")
            minimum_score = int(criterion.get("minimum_score", 1))
            maximum_score = int(criterion.get("maximum_score", 5))
            if minimum_score < 0 or maximum_score < minimum_score:
                raise HTTPException(status_code=400, detail="Each rubric criterion needs a valid score range")
            connection.execute(
                """INSERT INTO criteria
                   (event_id, name, description, weight, sort_order, minimum_score, maximum_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_id, name, description, weight, int(criterion.get("sort_order", index)), minimum_score, maximum_score),
            )
        for index, question in enumerate(body.custom_questions, start=1):
            question_key = str(question.get("key", question.get("question_key", ""))).strip()
            prompt = str(question.get("prompt", "")).strip()
            if len(question_key) < 2 or len(prompt) < 2:
                raise HTTPException(status_code=400, detail="Custom questions need a key and prompt")
            connection.execute(
                """INSERT INTO custom_questions
                   (event_id, question_key, prompt, question_type, required, options, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, question_key, prompt, str(question.get("type", "text")),
                    int(bool(question.get("required", False))),
                    json.dumps(question.get("options", [])), index,
                ),
            )
        audit(connection, user["id"], "create", "event", str(event_id))
        event = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        dispatch_webhook(connection, "event.created", {"event_id": event_id, "name": body.name})
    return {"event": row_dict(event)}


@app.put("/api/events/{event_id}")
def update_event(
    event_id: int,
    body: EventBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    if body.voting_access not in {"open", "email", "authenticated"}:
        raise HTTPException(status_code=400, detail="Voting access must be open, email, or authenticated")
    if body.voting_mode not in {"one_per_project", "one_per_event", "quadratic"}:
        raise HTTPException(status_code=400, detail="Voting mode is invalid")
    if body.status not in {"draft", "active", "archived"}:
        raise HTTPException(status_code=400, detail="Event status is invalid")
    if body.team_min_size > body.team_max_size:
        raise HTTPException(status_code=400, detail="Minimum team size cannot exceed maximum team size")
    starts_at = normalize_time(body.starts_at, "Event start")
    deadline = normalize_time(body.deadline, "Event deadline")
    optional_dates = {
        "registration_opens_at": normalize_optional_time(body.registration_opens_at, "Registration start"),
        "registration_closes_at": normalize_optional_time(body.registration_closes_at, "Registration end"),
        "hackathon_ends_at": normalize_optional_time(body.hackathon_ends_at, "Hackathon end"),
        "judging_opens_at": normalize_optional_time(body.judging_opens_at, "Judging start"),
        "judging_closes_at": normalize_optional_time(body.judging_closes_at, "Judging end"),
        "results_publish_at": normalize_optional_time(body.results_publish_at, "Results publication date"),
        "voting_opens_at": normalize_optional_time(body.voting_opens_at, "Voting start"),
        "voting_closes_at": normalize_optional_time(body.voting_closes_at, "Voting end"),
    }
    validate_event_timeline(
        starts_at, deadline, optional_dates["registration_opens_at"], optional_dates["registration_closes_at"],
        optional_dates["hackathon_ends_at"], optional_dates["judging_opens_at"],
        optional_dates["judging_closes_at"], optional_dates["results_publish_at"],
    )
    if optional_dates["voting_opens_at"] and optional_dates["voting_closes_at"] and parse_optional_time(optional_dates["voting_closes_at"]) <= parse_optional_time(optional_dates["voting_opens_at"]):
        raise HTTPException(status_code=400, detail="Voting close must be after voting open")
    with db() as connection:
        if not connection.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        connection.execute(
            """UPDATE events SET name = ?, tagline = ?, description = ?, starts_at = ?, deadline = ?,
               registration_opens_at = ?, registration_closes_at = ?, hackathon_ends_at = ?,
               judging_opens_at = ?, judging_closes_at = ?, results_publish_at = ?, status = ?,
               voting_access = ?, voting_mode = ?, voting_opens_at = ?, voting_closes_at = ?,
               results_visible = ?, feedback_visible = ?, quadratic_budget = ?, team_min_size = ?, team_max_size = ?,
               comments_enabled = ? WHERE id = ?""",
            (
                body.name.strip(), body.tagline.strip(), body.description.strip(), starts_at, deadline,
                optional_dates["registration_opens_at"], optional_dates["registration_closes_at"],
                optional_dates["hackathon_ends_at"], optional_dates["judging_opens_at"],
                optional_dates["judging_closes_at"], optional_dates["results_publish_at"], body.status,
                body.voting_access, body.voting_mode, optional_dates["voting_opens_at"],
                 optional_dates["voting_closes_at"], int(body.results_visible), int(body.feedback_visible), body.quadratic_budget,
                body.team_min_size, body.team_max_size, int(body.comments_enabled), event_id,
            ),
        )
        existing_tracks = connection.execute(
            "SELECT id FROM tracks WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()
        for index, track in enumerate(body.tracks):
            name = str(track.get("name", "")).strip()
            if not name:
                continue
            color = str(track.get("color", "")).strip() or ["#ff3b86", "#25e0c2", "#a78bfa"][index % 3]
            if index < len(existing_tracks):
                connection.execute(
                    "UPDATE tracks SET name = ?, color = ? WHERE id = ?",
                    (name, color, existing_tracks[index]["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO tracks (event_id, name, color) VALUES (?, ?, ?)",
                    (event_id, name, color),
                )
        existing_prizes = connection.execute(
            "SELECT id FROM prizes WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()
        for index, prize in enumerate(body.prizes):
            title = str(prize.get("title", "")).strip()
            if not title:
                continue
            amount = str(prize.get("amount", "")).strip()
            if index < len(existing_prizes):
                connection.execute(
                    "UPDATE prizes SET title = ?, amount = ? WHERE id = ?",
                    (title, amount, existing_prizes[index]["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO prizes (event_id, title, amount) VALUES (?, ?, ?)",
                    (event_id, title, amount),
                )
        connection.execute("DELETE FROM custom_questions WHERE event_id = ?", (event_id,))
        for index, question in enumerate(body.custom_questions, start=1):
            key = str(question.get("key", question.get("question_key", ""))).strip()
            prompt = str(question.get("prompt", "")).strip()
            if len(key) < 2 or len(prompt) < 2:
                raise HTTPException(status_code=400, detail="Custom questions need a key and prompt")
            connection.execute(
                """INSERT INTO custom_questions
                   (event_id, question_key, prompt, question_type, required, options, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, key, prompt, str(question.get("type", "text")),
                    int(bool(question.get("required", False))),
                    json.dumps(question.get("options", [])), index,
                ),
            )
        audit(connection, user["id"], "update", "event", str(event_id), body.model_dump())
        event = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return {"event": row_dict(event), "phase": event_phase(event)}


@app.put("/api/organizer/events/{event_id}/criteria")
def update_criteria(
    event_id: int,
    body: CriteriaUpdateBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    if sum(item.weight for item in body.criteria) != 100:
        raise HTTPException(status_code=400, detail="Rubric weights must add up to 100")
    ids = [item.id for item in body.criteria]
    if any(item_id is None for item_id in ids) or len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Each existing criterion must be included exactly once")
    with db() as connection:
        event = connection.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        existing = {
            row["id"]
            for row in connection.execute("SELECT id FROM criteria WHERE event_id = ?", (event_id,)).fetchall()
        }
        if set(ids) != existing:
            raise HTTPException(status_code=400, detail="Rubric criteria cannot be added or removed from this screen")
        for item in body.criteria:
            if item.maximum_score < item.minimum_score:
                raise HTTPException(status_code=400, detail="Each criterion needs a valid score range")
            connection.execute(
                """UPDATE criteria
                   SET name = ?, description = ?, weight = ?, sort_order = ?, minimum_score = ?, maximum_score = ?
                   WHERE id = ? AND event_id = ?""",
                (
                    item.name.strip(), item.description.strip(), item.weight, item.sort_order,
                    item.minimum_score, item.maximum_score, item.id, event_id,
                ),
            )
        audit(connection, user["id"], "update", "rubric", str(event_id), {
            "criteria": [item.model_dump() for item in body.criteria],
        })
        criteria = rows_dict(connection.execute(
            "SELECT * FROM criteria WHERE event_id = ? ORDER BY sort_order, id", (event_id,)
        ).fetchall())
    return {"criteria": criteria}


@app.put("/api/organizer/events/{event_id}/voting")
def update_voting_policy(
    event_id: int,
    body: VotingPolicyBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    if body.voting_access not in {"open", "email", "authenticated"}:
        raise HTTPException(status_code=400, detail="Voting access must be open, email, or authenticated")
    if body.voting_mode not in {"one_per_project", "one_per_event", "quadratic"}:
        raise HTTPException(status_code=400, detail="Voting mode is invalid")
    try:
        opens_at = parse_optional_time(body.voting_opens_at)
        closes_at = parse_optional_time(body.voting_closes_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Voting dates must be valid ISO dates") from exc
    if opens_at and closes_at and closes_at <= opens_at:
        raise HTTPException(status_code=400, detail="Voting close must be after voting open")
    with db() as connection:
        if not connection.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        connection.execute(
            """UPDATE events SET voting_access = ?, voting_mode = ?, voting_opens_at = ?,
               voting_closes_at = ?, results_visible = ?, quadratic_budget = ? WHERE id = ?""",
            (
                body.voting_access, body.voting_mode,
                opens_at.isoformat() if opens_at else None, closes_at.isoformat() if closes_at else None,
                int(body.results_visible), body.quadratic_budget, event_id,
            ),
        )
        audit(connection, user["id"], "update", "voting_policy", str(event_id), body.model_dump())
        dispatch_webhook(connection, "voting_policy.updated", {"event_id": event_id, **body.model_dump()})
        event = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return {"event": row_dict(event)}


@app.put("/api/organizer/events/{event_id}/feedback")
def update_feedback_policy(
    event_id: int,
    body: FeedbackPolicyBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        connection.execute(
            "UPDATE events SET feedback_visible = ? WHERE id = ?",
            (int(body.feedback_visible), event_id),
        )
        audit(connection, user["id"], "update", "feedback_policy", str(event_id), body.model_dump())
        event = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return {"event": row_dict(event)}


@app.get("/api/gallery")
def gallery(
    q: str = "",
    track: str = "",
    event_id: int | None = None,
    request: Request = None,
    user: sqlite3.Row | None = Depends(optional_current_user),
) -> dict[str, Any]:
    with db() as connection:
        selected_event = connection.execute(
            "SELECT * FROM events WHERE id = ? OR (? IS NULL AND id = (SELECT id FROM events ORDER BY id DESC LIMIT 1))",
            (event_id, event_id),
        ).fetchone()
        if not selected_event:
            return {"items": [], "tracks": [], "voting": {}}
        selected_event_id = selected_event["id"]
        policy = voting_policy(selected_event, request, user)
        query = """
          SELECT p.*, t.name AS track_name, t.color AS track_color, te.name AS team_name,
            COALESCE((SELECT SUM(v.quantity) FROM votes v WHERE v.project_id = p.id), 0) AS votes,
            (SELECT AVG(s.score * 1.0) FROM scores s JOIN assignments a ON a.id = s.assignment_id WHERE a.project_id = p.id) AS judge_average
          FROM projects p JOIN tracks t ON t.id = p.track_id JOIN teams te ON te.id = p.team_id
          WHERE p.status = 'submitted' AND p.event_id = ?
        """
        params: list[Any] = [selected_event_id]
        if q:
            query += " AND (lower(p.title) LIKE ? OR lower(p.summary) LIKE ? OR lower(te.name) LIKE ? OR lower(p.tech_tags) LIKE ?)"
            params.extend([f"%{q.lower()}%"] * 4)
        if track:
            query += " AND p.track_id = ?"
            params.append(track)
        if event_id:
            query += " AND p.event_id = ?"
            params.append(selected_event_id)
        query += " ORDER BY abs(random())"
        items = [public_project(row) for row in connection.execute(query, params).fetchall()]
        tracks = rows_dict(connection.execute("SELECT id, name, color FROM tracks WHERE event_id = ?", (selected_event_id,)).fetchall())
    if not policy["results_revealed"]:
        for item in items:
            item["votes"] = None
            item["judge_average"] = None
    if not selected_event["results_published"]:
        for item in items:
            item["judge_average"] = None
    return {
        "items": items,
        "tracks": tracks,
        "voting": {
            key: policy[key]
            for key in ("access", "mode", "opens_at", "closes_at", "can_vote", "results_revealed", "window_closed", "quadratic_budget")
        },
    }


@app.get("/projects")
def dogfood_gallery(request: Request) -> dict[str, Any]:
    """Compatibility alias declared in .dogfood.toml for the shared checker."""
    return gallery(request=request, user=None)


@app.post("/projects/new")
def dogfood_submit_probe(
    user: sqlite3.Row = Depends(role_required("participant", "organizer", "admin")),
) -> dict[str, Any]:
    """Compatibility endpoint that applies the normal event deadline guard."""
    with db() as connection:
        event = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
    if not event:
        raise HTTPException(status_code=404, detail="No event is available")
    if deadline_passed(event["deadline"]):
        raise HTTPException(status_code=400, detail="Submissions are closed")
    raise HTTPException(status_code=409, detail="Use the project submission form")


@app.get("/api/projects/{project_id}")
def project_detail(
    project_id: int,
    request: Request,
    user: sqlite3.Row | None = Depends(optional_current_user),
) -> dict[str, Any]:
    with db() as connection:
        project = connection.execute(
            """SELECT p.*, t.name AS track_name, t.color AS track_color, te.name AS team_name
               FROM projects p JOIN tracks t ON t.id = p.track_id JOIN teams te ON te.id = p.team_id WHERE p.id = ?""",
            (project_id,),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if user and user["role"] == "judge":
            assigned = connection.execute(
                "SELECT 1 FROM assignments WHERE project_id = ? AND judge_id = ?",
                (project_id, user["id"]),
            ).fetchone()
            if not assigned:
                raise HTTPException(status_code=403, detail="This project is not assigned to you")
        elif project["status"] != "submitted" and not (
            user and user["role"] in {"organizer", "admin"}
        ):
            if not user or user["role"] != "participant" or not participant_team(connection, user["id"], project["team_id"]):
                raise HTTPException(status_code=404, detail="Project not found")
        elif project["status"] != "submitted" and user["role"] == "judge":
            raise HTTPException(status_code=403, detail="This project is not available for judging")
        event = connection.execute("SELECT * FROM events WHERE id = ?", (project["event_id"],)).fetchone()
        policy = voting_policy(event, request, user)
        comments = rows_dict(connection.execute(
            "SELECT * FROM comments WHERE project_id = ? AND status = 'active' ORDER BY created_at DESC",
            (project_id,),
        ).fetchall())
        votes = connection.execute("SELECT COALESCE(SUM(quantity), 0) FROM votes WHERE project_id = ?", (project_id,)).fetchone()[0]
    return {
        "project": public_project(project),
        "comments": comments,
        "comments_enabled": bool(event["comments_enabled"]),
        "votes": votes if policy["results_revealed"] else None,
        "results_hidden": not policy["results_revealed"],
        "voting": {
            key: policy[key]
            for key in ("access", "mode", "opens_at", "closes_at", "can_vote", "results_revealed", "window_closed", "quadratic_budget")
        },
    }


def participant_team(connection: sqlite3.Connection, user_id: int, team_id: int) -> bool:
    return connection.execute("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_id)).fetchone() is not None


@app.post("/api/projects")
def create_project(body: ProjectBody, user: sqlite3.Row = Depends(role_required("participant", "organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        team = connection.execute("SELECT * FROM teams WHERE id = ? AND event_id = ?", (body.team_id, body.event_id)).fetchone()
        track = connection.execute("SELECT * FROM tracks WHERE id = ? AND event_id = ?", (body.track_id, body.event_id)).fetchone()
        if not team or not track:
            raise HTTPException(status_code=400, detail="Team and track must belong to the selected event")
        if body.status not in {"draft", "submitted", "locked"}:
            raise HTTPException(status_code=400, detail="Project status must be draft, submitted, or locked")
        if body.status == "locked" and user["role"] == "participant":
            raise HTTPException(status_code=403, detail="Participants cannot lock a project")
        if user["role"] == "participant" and not participant_team(connection, user["id"], body.team_id):
            raise HTTPException(status_code=403, detail="You can only submit for your own team")
        if connection.execute(
            "SELECT 1 FROM projects WHERE team_id = ? AND event_id = ?",
            (body.team_id, body.event_id),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Each team can submit only one project")
        if user["role"] == "participant" and submissions_closed(event):
            raise HTTPException(status_code=400, detail="The submission deadline has passed")
        if user["role"] == "participant" and body.status == "submitted":
            member_count = connection.execute(
                "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (body.team_id,)
            ).fetchone()[0]
            if member_count < int(event["team_min_size"] or 1):
                raise HTTPException(
                    status_code=400,
                    detail=f"Teams must have at least {event['team_min_size']} member(s) before submitting",
                )
        validate_custom_answers(connection, body.event_id, body.custom_answers)
        slug = "-".join(body.title.lower().split()) + "-" + secrets.token_hex(2)
        submitted = now() if body.status == "submitted" else None
        cursor = connection.execute(
            """INSERT INTO projects
               (event_id, team_id, track_id, title, slug, summary, tagline, long_description,
                thumbnail_url, image_urls, demo_video_url, tech_tags, custom_answers,
                repo_url, demo_url, status, submitted_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                body.event_id, body.team_id, body.track_id, body.title, slug, body.summary,
                body.tagline, body.long_description, body.thumbnail_url, json.dumps(body.image_urls),
                body.demo_video_url, json.dumps(body.tech_tags), json.dumps(body.custom_answers),
                body.repo_url, body.demo_url, body.status, submitted, now(),
            ),
        )
        audit(connection, user["id"], "create", "project", str(cursor.lastrowid), {"status": body.status})
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)).fetchone()
        dispatch_webhook(connection, "project.created", {"event_id": body.event_id, "project_id": cursor.lastrowid, "status": body.status})
    return {"project": public_project(project)}


@app.put("/api/projects/{project_id}")
def update_project(project_id: int, body: ProjectBody, user: sqlite3.Row = Depends(role_required("participant", "organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        event = connection.execute("SELECT * FROM events WHERE id = ?", (project["event_id"],)).fetchone()
        if body.event_id != project["event_id"]:
            raise HTTPException(status_code=400, detail="A project cannot be moved between events")
        team = connection.execute(
            "SELECT id FROM teams WHERE id = ? AND event_id = ?", (body.team_id, project["event_id"])
        ).fetchone()
        if not team:
            raise HTTPException(status_code=400, detail="Team must belong to the project's event")
        if user["role"] == "participant" and not participant_team(connection, user["id"], project["team_id"]):
            raise HTTPException(status_code=403, detail="You can only edit your own project")
        if user["role"] == "participant" and body.team_id != project["team_id"]:
            raise HTTPException(status_code=403, detail="You can only edit your own team's project")
        if user["role"] == "participant" and project["status"] == "locked":
            raise HTTPException(status_code=403, detail="This submission is locked by the organizer")
        if user["role"] == "participant" and submissions_closed(event):
            raise HTTPException(status_code=400, detail="The submission deadline has passed")
        if user["role"] == "participant" and body.status == "submitted":
            member_count = connection.execute(
                "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (project["team_id"],)
            ).fetchone()[0]
            if member_count < int(event["team_min_size"] or 1):
                raise HTTPException(
                    status_code=400,
                    detail=f"Teams must have at least {event['team_min_size']} member(s) before submitting",
                )
        track = connection.execute("SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (body.track_id, project["event_id"])).fetchone()
        if not track or body.status not in {"draft", "submitted", "locked"}:
            raise HTTPException(status_code=400, detail="Track or project status is invalid")
        if body.status == "locked" and user["role"] == "participant":
            raise HTTPException(status_code=403, detail="Participants cannot lock a project")
        if body.team_id != project["team_id"] and connection.execute(
            "SELECT 1 FROM projects WHERE team_id = ? AND event_id = ? AND id != ?",
            (body.team_id, project["event_id"], project_id),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Each team can submit only one project")
        validate_custom_answers(connection, project["event_id"], body.custom_answers)
        submitted = project["submitted_at"] or (now() if body.status == "submitted" else None)
        connection.execute(
            """UPDATE projects SET track_id = ?, title = ?, summary = ?, tagline = ?, long_description = ?,
               team_id = ?, thumbnail_url = ?, image_urls = ?, demo_video_url = ?, tech_tags = ?, custom_answers = ?,
               repo_url = ?, demo_url = ?, status = ?, submitted_at = ?, updated_at = ? WHERE id = ?""",
            (
                body.track_id, body.title, body.summary, body.tagline, body.long_description, body.team_id,
                body.thumbnail_url, json.dumps(body.image_urls), body.demo_video_url,
                json.dumps(body.tech_tags), json.dumps(body.custom_answers), body.repo_url,
                body.demo_url, body.status, submitted, now(), project_id,
            ),
        )
        audit(connection, user["id"], "update", "project", str(project_id), {"status": body.status})
        updated = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        dispatch_webhook(connection, "project.updated", {"event_id": project["event_id"], "project_id": project_id, "status": body.status})
    return {"project": public_project(updated)}


@app.get("/api/me/projects")
def my_projects(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        if user["role"] == "participant":
            project_rows = connection.execute(
                """SELECT p.*, t.name AS track_name, te.name AS team_name FROM projects p
                   JOIN teams te ON te.id = p.team_id JOIN tracks t ON t.id = p.track_id
                   JOIN team_members tm ON tm.team_id = p.team_id WHERE tm.user_id = ? ORDER BY p.updated_at DESC""",
                (user["id"],),
            ).fetchall()
        else:
            project_rows = connection.execute(
                "SELECT p.*, t.name AS track_name, te.name AS team_name FROM projects p JOIN teams te ON te.id = p.team_id JOIN tracks t ON t.id = p.track_id ORDER BY p.updated_at DESC",
            ).fetchall()
        items = [public_project(row) for row in project_rows]
    return {"items": items}


@app.get("/api/me/team")
def my_team(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        team = connection.execute(
            """SELECT te.* FROM teams te JOIN team_members tm ON tm.team_id = te.id
               WHERE tm.user_id = ? ORDER BY te.id DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
        if not team:
            return {"team": None, "members": []}
        members = rows_dict(connection.execute(
            "SELECT u.id, u.name, u.email, tm.member_role FROM team_members tm JOIN users u ON u.id = tm.user_id WHERE tm.team_id = ?",
            (team["id"],),
        ).fetchall())
    return {"team": row_dict(team), "members": members}


@app.post("/api/teams")
def create_team(body: TeamBody, user: sqlite3.Row = Depends(role_required("participant"))) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        if registration_closed(event):
            raise HTTPException(status_code=400, detail="Team registration is closed for this event")
        if connection.execute(
            """SELECT 1 FROM team_members tm JOIN teams te ON te.id = tm.team_id
               WHERE tm.user_id = ? AND te.event_id = ?""",
            (user["id"], body.event_id),
        ).fetchone():
            raise HTTPException(status_code=409, detail="You already belong to a team for this event")
        cursor = connection.execute(
            "INSERT INTO teams (event_id, name, invite_code, created_at) VALUES (?, ?, ?, ?)",
            (body.event_id, body.name, secrets.token_urlsafe(8), now()),
        )
        connection.execute(
            "INSERT INTO team_members (team_id, user_id, member_role) VALUES (?, ?, 'captain')",
            (cursor.lastrowid, user["id"]),
        )
        audit(connection, user["id"], "create", "team", str(cursor.lastrowid))
        team = connection.execute("SELECT * FROM teams WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"team": row_dict(team)}


@app.get("/api/teams")
def list_teams(
    event_id: int | None = None,
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    with db() as connection:
        selected_event = event_id or connection.execute(
            "SELECT id FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        selected_id = selected_event["id"] if selected_event else None
        if not selected_id:
            return {"items": []}
        if user["role"] in {"organizer", "admin"}:
            rows = connection.execute(
                """SELECT te.id, te.event_id, te.name, te.invite_code, te.created_at,
                          COUNT(tm.user_id) AS member_count
                   FROM teams te LEFT JOIN team_members tm ON tm.team_id = te.id
                   WHERE te.event_id = ? GROUP BY te.id ORDER BY te.name""",
                (selected_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT te.id, te.event_id, te.name, te.invite_code, te.created_at,
                          COUNT(tm.user_id) AS member_count
                   FROM teams te JOIN team_members mine ON mine.team_id = te.id
                   LEFT JOIN team_members tm ON tm.team_id = te.id
                   WHERE te.event_id = ? AND mine.user_id = ?
                   GROUP BY te.id ORDER BY te.name""",
                (selected_id, user["id"]),
            ).fetchall()
    return {"items": rows_dict(rows)}


@app.post("/api/teams/{team_id}/invite")
def invite_team_member(
    team_id: int,
    body: TeamInviteBody,
    user: sqlite3.Row = Depends(role_required("participant")),
) -> dict[str, Any]:
    with db() as connection:
        team = connection.execute(
            "SELECT * FROM teams WHERE id = ?", (team_id,)
        ).fetchone()
        if not team or not participant_team(connection, user["id"], team_id):
            raise HTTPException(status_code=403, detail="You can only invite members to your own team")
        event = connection.execute(
            "SELECT * FROM events WHERE id = ?", (team["event_id"],)
        ).fetchone()
        if registration_closed(event):
            raise HTTPException(status_code=400, detail="Team registration is closed for this event")
        member_count = connection.execute(
            "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team_id,)
        ).fetchone()[0]
        if member_count >= int(event["team_max_size"] or 4):
            raise HTTPException(status_code=409, detail="This team has reached its maximum size")
        if body.email:
            target = connection.execute(
                "SELECT id, email, name FROM users WHERE lower(email) = lower(?)",
                (body.email.strip(),),
            ).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="Create an account before joining this team")
            if connection.execute(
                "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                (team_id, target["id"]),
            ).fetchone():
                raise HTTPException(status_code=409, detail="That user is already on this team")
        audit(connection, user["id"], "invite", "team", str(team_id), {"email": body.email or ""})
    return {"invite_code": team["invite_code"], "team": row_dict(team)}


@app.post("/api/teams/join")
def join_team(body: TeamJoinBody, user: sqlite3.Row = Depends(role_required("participant"))) -> dict[str, Any]:
    with db() as connection:
        team = connection.execute("SELECT * FROM teams WHERE invite_code = ?", (body.invite_code.strip(),)).fetchone()
        if not team:
            raise HTTPException(status_code=404, detail="Invite link is not valid")
        event = connection.execute("SELECT * FROM events WHERE id = ?", (team["event_id"],)).fetchone()
        if registration_closed(event):
            raise HTTPException(status_code=400, detail="Team registration is closed for this event")
        member_count = connection.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team["id"],)).fetchone()[0]
        if member_count >= int(event["team_max_size"] or 4):
            raise HTTPException(status_code=409, detail="This team has reached its maximum size")
        if connection.execute(
            """SELECT 1 FROM team_members tm JOIN teams te ON te.id = tm.team_id
               WHERE tm.user_id = ? AND te.event_id = ?""",
            (user["id"], team["event_id"]),
        ).fetchone():
            raise HTTPException(status_code=409, detail="You already belong to a team for this event")
        connection.execute(
            "INSERT INTO team_members (team_id, user_id, member_role) VALUES (?, ?, 'member')",
            (team["id"], user["id"]),
        )
        audit(connection, user["id"], "join", "team", str(team["id"]))
    return {"team": row_dict(team)}


@app.delete("/api/teams/{team_id}/members/me")
def leave_team(team_id: int, user: sqlite3.Row = Depends(role_required("participant"))) -> dict[str, Any]:
    with db() as connection:
        membership = connection.execute(
            "SELECT member_role FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, user["id"]),
        ).fetchone()
        if not membership:
            raise HTTPException(status_code=404, detail="You are not a member of this team")
        members = connection.execute(
            "SELECT user_id FROM team_members WHERE team_id = ? AND user_id != ? ORDER BY user_id",
            (team_id, user["id"]),
        ).fetchall()
        if not members and connection.execute("SELECT 1 FROM projects WHERE team_id = ?", (team_id,)).fetchone():
            raise HTTPException(status_code=409, detail="A team with a project cannot be left without another member")
        connection.execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user["id"]))
        if membership["member_role"] == "captain" and members:
            connection.execute(
                "UPDATE team_members SET member_role = 'captain' WHERE team_id = ? AND user_id = ?",
                (team_id, members[0]["user_id"]),
            )
        audit(connection, user["id"], "leave", "team", str(team_id))
        remaining = connection.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team_id,)).fetchone()[0]
        if remaining == 0:
            connection.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    return {"ok": True}


def assigned_project_ids(connection: sqlite3.Connection, judge_id: int) -> set[int]:
    return {row[0] for row in connection.execute("SELECT project_id FROM assignments WHERE judge_id = ?", (judge_id,)).fetchall()}


def judge_can_review_track(connection: sqlite3.Connection, event_id: int, judge_id: int, track_id: int) -> bool:
    authorized = connection.execute(
        "SELECT 1 FROM judge_tracks WHERE event_id = ? AND judge_id = ? LIMIT 1",
        (event_id, judge_id),
    ).fetchone()
    if not authorized:
        return True
    return connection.execute(
        "SELECT 1 FROM judge_tracks WHERE event_id = ? AND judge_id = ? AND track_id = ?",
        (event_id, judge_id, track_id),
    ).fetchone() is not None


@app.get("/api/judge/assignments")
def judge_assignments(user: sqlite3.Row = Depends(role_required("judge", "organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        if user["role"] == "judge":
            query = """
              SELECT p.*, t.name AS track_name, t.color AS track_color, te.name AS team_name,
                (SELECT COUNT(*) FROM scores s JOIN assignments aa ON aa.id = s.assignment_id WHERE aa.project_id = p.id AND aa.judge_id = ?) AS scored_criteria,
                (SELECT COUNT(*) FROM criteria c WHERE c.event_id = p.event_id) AS criteria_count
              FROM projects p JOIN tracks t ON t.id = p.track_id JOIN teams te ON te.id = p.team_id
              JOIN assignments a ON a.project_id = p.id AND a.judge_id = ? ORDER BY p.title
            """
            items = rows_dict(connection.execute(query, (user["id"], user["id"])).fetchall())
        else:
            items = rows_dict(connection.execute(
                """SELECT p.*, t.name AS track_name, te.name AS team_name, u.name AS judge_name,
                   (SELECT COUNT(*) FROM scores s WHERE s.assignment_id = a.id) AS scored_criteria,
                   (SELECT COUNT(*) FROM criteria c WHERE c.event_id = p.event_id) AS criteria_count
                   FROM assignments a JOIN projects p ON p.id = a.project_id JOIN tracks t ON t.id = p.track_id
                   JOIN teams te ON te.id = p.team_id JOIN users u ON u.id = a.judge_id ORDER BY p.title""",
            ).fetchall())
        summary = None
        if user["role"] == "judge":
            total = len(items)
            completed = sum(1 for item in items if item["scored_criteria"] >= item["criteria_count"])
            summary = {
                "total": total,
                "completed": completed,
                "remaining": max(0, total - completed),
                "completion_percent": round((completed / total) * 100) if total else 100,
            }
    return {"items": items, "summary": summary}


@app.get("/api/organizer/judge-invitations")
def judge_invitations(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        items = rows_dict(connection.execute(
            """SELECT ji.*, e.name AS event_name, u.name AS invited_by_name FROM judge_invitations ji
               JOIN events e ON e.id = ji.event_id JOIN users u ON u.id = ji.invited_by ORDER BY ji.created_at DESC""",
        ).fetchall())
    return {"items": items}


@app.post("/api/organizer/judge-invitations")
def invite_judge(body: JudgeInvitationBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        invitation = connection.execute(
            "INSERT INTO judge_invitations (event_id, email, invited_by, token, created_at) VALUES (?, ?, ?, ?, ?)",
            (body.event_id, body.email.strip().lower(), user["id"], secrets.token_urlsafe(18), now()),
        )
        audit(connection, user["id"], "invite", "judge", str(invitation.lastrowid), {"email": body.email})
        item = connection.execute("SELECT * FROM judge_invitations WHERE id = ?", (invitation.lastrowid,)).fetchone()
    return {"invitation": row_dict(item)}


@app.get("/api/judge-invitations/{token}")
def invitation_detail(token: str) -> dict[str, Any]:
    with db() as connection:
        invitation = connection.execute(
            """SELECT ji.id, ji.email, ji.status, ji.created_at, e.id AS event_id, e.name AS event_name
               FROM judge_invitations ji JOIN events e ON e.id = ji.event_id
               WHERE ji.token = ?""",
            (token,),
        ).fetchone()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    return {"invitation": row_dict(invitation)}


@app.post("/api/judge-invitations/{token}/accept")
def accept_judge_invitation(
    token: str,
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    with db() as connection:
        invitation = connection.execute(
            "SELECT * FROM judge_invitations WHERE token = ?", (token,)
        ).fetchone()
        if not invitation:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if invitation["status"] == "accepted":
            return {"ok": True, "event_id": invitation["event_id"]}
        if user["email"].lower() != invitation["email"].lower():
            raise HTTPException(status_code=403, detail="This invitation belongs to a different email address")
        if user["role"] not in {"participant", "judge"}:
            raise HTTPException(status_code=403, detail="This account cannot accept a judge invitation")
        connection.execute("UPDATE users SET role = 'judge' WHERE id = ?", (user["id"],))
        connection.execute(
            "UPDATE judge_invitations SET status = 'accepted' WHERE id = ?",
            (invitation["id"],),
        )
        audit(connection, user["id"], "accept", "judge_invitation", str(invitation["id"]))
    return {"ok": True, "event_id": invitation["event_id"]}


@app.post("/api/organizer/assignments")
def create_assignments(body: AssignmentBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        judge = connection.execute("SELECT id, role FROM users WHERE id = ? AND role = 'judge'", (body.judge_id,)).fetchone()
        if not judge:
            raise HTTPException(status_code=404, detail="Judge not found")
        created = 0
        for project_id in body.project_ids:
            project = connection.execute(
                "SELECT id, event_id, team_id, track_id, status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if not project:
                raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
            if project["status"] != "submitted":
                raise HTTPException(status_code=400, detail="Only submitted projects can be assigned")
            conflict = connection.execute(
                "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                (project["team_id"], body.judge_id),
            ).fetchone()
            if conflict:
                raise HTTPException(status_code=409, detail="A judge cannot score a project from their own team")
            if not judge_can_review_track(connection, project["event_id"], body.judge_id, project["track_id"]):
                raise HTTPException(status_code=403, detail="This judge is not authorized for the project's track")
            try:
                connection.execute("INSERT INTO assignments (project_id, judge_id) VALUES (?, ?)", (project_id, body.judge_id))
                created += 1
            except sqlite3.IntegrityError:
                pass
        audit(connection, user["id"], "assign", "judge", str(body.judge_id), {"projects": body.project_ids})
    return {"created": created}


@app.put("/api/organizer/judge-tracks")
def update_judge_tracks(
    body: JudgeTrackBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        judge = connection.execute(
            "SELECT id FROM users WHERE id = ? AND role = 'judge'", (body.judge_id,)
        ).fetchone()
        if not judge:
            raise HTTPException(status_code=404, detail="Judge not found")
        valid_tracks = {
            row["id"]
            for row in connection.execute(
                "SELECT id FROM tracks WHERE event_id = ?", (body.event_id,)
            ).fetchall()
        }
        if not set(body.track_ids).issubset(valid_tracks):
            raise HTTPException(status_code=400, detail="Every authorized track must belong to the event")
        connection.execute(
            "DELETE FROM judge_tracks WHERE event_id = ? AND judge_id = ?",
            (body.event_id, body.judge_id),
        )
        for track_id in body.track_ids:
            connection.execute(
                "INSERT INTO judge_tracks (event_id, judge_id, track_id) VALUES (?, ?, ?)",
                (body.event_id, body.judge_id, track_id),
            )
        audit(connection, user["id"], "update", "judge_tracks", str(body.judge_id), {
            "event_id": body.event_id, "track_ids": body.track_ids,
        })
    return {"event_id": body.event_id, "judge_id": body.judge_id, "track_ids": body.track_ids}


@app.post("/api/organizer/assignments/auto")
def auto_assignments(
    body: AutoAssignmentBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        judges = connection.execute("SELECT id FROM users WHERE role = 'judge' ORDER BY id").fetchall()
        projects = connection.execute(
            "SELECT id, event_id, team_id, track_id FROM projects WHERE event_id = ? AND status = 'submitted' ORDER BY id",
            (body.event_id,),
        ).fetchall()
        if not judges:
            raise HTTPException(status_code=400, detail="No judges are available for automatic assignment")
        created = 0
        incomplete: list[int] = []
        for project in projects:
            existing = {
                row["judge_id"]
                for row in connection.execute(
                    "SELECT judge_id FROM assignments WHERE project_id = ?", (project["id"],)
                ).fetchall()
            }
            candidates = []
            for judge in judges:
                if judge["id"] in existing:
                    continue
                if connection.execute(
                    "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                    (project["team_id"], judge["id"]),
                ).fetchone():
                    continue
                if not judge_can_review_track(connection, project["event_id"], judge["id"], project["track_id"]):
                    continue
                candidates.append(judge["id"])
            candidates.sort(key=lambda judge_id: (
                connection.execute(
                    "SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id = a.project_id WHERE a.judge_id = ? AND p.event_id = ?",
                    (judge_id, body.event_id),
                ).fetchone()[0],
                judge_id,
            ))
            needed = max(0, body.reviews_per_project - len(existing))
            for judge_id in candidates[:needed]:
                connection.execute(
                    "INSERT INTO assignments (project_id, judge_id) VALUES (?, ?)",
                    (project["id"], judge_id),
                )
                created += 1
            if len(existing) + min(len(candidates), needed) < body.reviews_per_project:
                incomplete.append(project["id"])
        audit(connection, user["id"], "auto_assign", "event", str(body.event_id), {
            "reviews_per_project": body.reviews_per_project, "created": created, "incomplete": incomplete,
        })
    return {"created": created, "projects": len(projects), "incomplete_project_ids": incomplete}


@app.get("/api/organizer/results")
def organizer_results(
    event_id: int | None = None,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        raw_rows = connection.execute(
            """SELECT a.judge_id, p.id AS project_id, p.title, te.name AS team_name, c.id AS criterion_id,
                      c.name AS criterion_name, c.weight, s.score, u.name AS judge_name
               FROM scores s JOIN assignments a ON a.id = s.assignment_id JOIN projects p ON p.id = a.project_id
               JOIN teams te ON te.id = p.team_id JOIN criteria c ON c.id = s.criterion_id JOIN users u ON u.id = a.judge_id
               WHERE (? IS NULL OR p.event_id = ?)""",
            (event_id, event_id),
        ).fetchall()
        rows_by_judge: dict[int, list[sqlite3.Row]] = {}
        for row in raw_rows:
            rows_by_judge.setdefault(row["judge_id"], []).append(row)

        judge_profiles: list[dict[str, Any]] = []
        normalized_rows: list[tuple[sqlite3.Row, float]] = []
        for judge_id, judge_rows in rows_by_judge.items():
            scores = [row["score"] for row in judge_rows]
            mean = sum(scores) / len(scores)
            variance = sum((score - mean) ** 2 for score in scores) / len(scores)
            stddev = variance ** 0.5 or 1
            profile = {
                "judge_id": judge_id,
                "judge_name": judge_rows[0]["judge_name"],
                "score_count": len(scores),
                "mean": round(mean, 4),
                "stddev": round(stddev, 4),
                "scores": [],
            }
            for row in judge_rows:
                normalized_score = max(0, min(100, round(50 + ((row["score"] - mean) / stddev) * 10, 2)))
                normalized_rows.append((row, normalized_score))
                profile["scores"].append(
                    {
                        "project_id": row["project_id"],
                        "project_title": row["title"],
                        "criterion_id": row["criterion_id"],
                        "criterion_name": row["criterion_name"],
                        "weight": row["weight"],
                        "raw_score": row["score"],
                        "normalized_score": normalized_score,
                        "raw_contribution": round(row["score"] * row["weight"] / 5, 2),
                        "normalized_contribution": round(normalized_score * row["weight"] / 100, 2),
                    }
                )
            judge_profiles.append(profile)

        result_map: dict[int, dict[str, Any]] = {}
        for row, normalized_score in normalized_rows:
            project = result_map.setdefault(
                row["project_id"],
                {
                    "project_id": row["project_id"],
                    "title": row["title"],
                    "team_name": row["team_name"],
                    "raw_total": 0.0,
                    "normalized_total": 0.0,
                    "score_count": 0,
                    "judges": set(),
                },
            )
            project["raw_total"] += row["score"] * row["weight"] / 5
            project["normalized_total"] += normalized_score * row["weight"] / 100
            project["score_count"] += 1
            project["judges"].add(row["judge_name"])
        results = []
        for item in result_map.values():
            item["raw_score"] = round(item.pop("raw_total") / max(1, len(item["judges"])), 2)
            item["normalized_score"] = round(item.pop("normalized_total") / max(1, len(item["judges"])), 2)
            item["judges"] = sorted(item["judges"])
            results.append(item)

        raw_ranking = sorted(results, key=lambda item: (-item["raw_score"], item["title"].lower()))
        normalized_ranking = sorted(results, key=lambda item: (-item["normalized_score"], item["title"].lower()))
        raw_ranks = {item["project_id"]: index + 1 for index, item in enumerate(raw_ranking)}
        normalized_ranks = {item["project_id"]: index + 1 for index, item in enumerate(normalized_ranking)}
        for item in results:
            item["raw_rank"] = raw_ranks[item["project_id"]]
            item["normalized_rank"] = normalized_ranks[item["project_id"]]
            item["rank_delta"] = item["raw_rank"] - item["normalized_rank"]
        results = normalized_ranking
        ranking_changes = [
            {
                "project_id": item["project_id"],
                "title": item["title"],
                "raw_score": item["raw_score"],
                "normalized_score": item["normalized_score"],
                "raw_rank": item["raw_rank"],
                "normalized_rank": item["normalized_rank"],
                "rank_delta": item["rank_delta"],
            }
            for item in results
        ]
    return {
        "items": results,
        "event_id": event_id,
        "normalization": {
            "method": "Per-judge z-score standardization",
            "formula": "normalized = clamp(50 + 10 × ((raw - judge_mean) / judge_stddev), 0, 100)",
            "midpoint": 50,
            "spread": 10,
            "clamp": [0, 100],
            "judge_profiles": sorted(judge_profiles, key=lambda item: item["judge_name"].lower()),
            "ranking_changes": ranking_changes,
        },
    }


@app.post("/api/organizer/results/publish")
def publish_results(
    body: ResultsPublishBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        connection.execute(
            "UPDATE events SET results_published = 1, results_visible = 1 WHERE id = ?",
            (body.event_id,),
        )
        audit(connection, user["id"], "publish", "results", str(body.event_id))
        updated = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
    return {"published": True, "event": row_dict(updated), "phase": event_phase(updated)}


@app.post("/api/organizer/results/unpublish")
def unpublish_results(
    body: ResultsPublishBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM events WHERE id = ?", (body.event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        connection.execute("UPDATE events SET results_published = 0 WHERE id = ?", (body.event_id,))
        audit(connection, user["id"], "unpublish", "results", str(body.event_id))
        event = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
    return {"published": False, "event": row_dict(event), "phase": event_phase(event)}


@app.get("/api/results")
def public_results(
    event_id: int | None = None,
    user: sqlite3.Row | None = Depends(optional_current_user),
) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute(
            """SELECT * FROM events
               WHERE id = ? OR (? IS NULL AND id = (SELECT id FROM events ORDER BY id DESC LIMIT 1))""",
            (event_id, event_id),
        ).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        if not event["results_published"] and not (user and user["role"] in {"organizer", "admin"}):
            return {"published": False, "event": row_dict(event), "items": []}
        organizer = user
        if not organizer or organizer["role"] not in {"organizer", "admin"}:
            organizer = connection.execute(
                "SELECT * FROM users WHERE role IN ('organizer', 'admin') ORDER BY id LIMIT 1"
            ).fetchone()
    result = organizer_results(event_id=event["id"], user=organizer)
    with db() as connection:
        awards = rows_dict(connection.execute(
            """SELECT ra.id, ra.prize_id, ra.project_id, ra.title, ra.recipient_name,
                      p.title AS project_title, pr.amount
               FROM result_awards ra
               LEFT JOIN projects p ON p.id = ra.project_id
               LEFT JOIN prizes pr ON pr.id = ra.prize_id
               WHERE ra.event_id = ? ORDER BY ra.id""",
            (event["id"],),
        ).fetchall())
        feedback: dict[int, list[dict[str, Any]]] = {}
        if event["feedback_visible"] or (user and user["role"] in {"organizer", "admin"}):
            feedback_rows = connection.execute(
                """SELECT p.id AS project_id, u.name AS judge_name, c.name AS criterion, s.note
                   FROM scores s JOIN assignments a ON a.id = s.assignment_id
                   JOIN projects p ON p.id = a.project_id JOIN users u ON u.id = a.judge_id
                   JOIN criteria c ON c.id = s.criterion_id
                   WHERE p.event_id = ? AND trim(s.note) <> ''
                   ORDER BY p.id, u.name, c.sort_order""",
                (event["id"],),
            ).fetchall()
            for row in feedback_rows:
                feedback.setdefault(row["project_id"], []).append({
                    "judge_name": row["judge_name"], "criterion": row["criterion"], "note": row["note"],
                })
    for item in result["items"]:
        item["awards"] = [award for award in awards if award["project_id"] == item["project_id"]]
        item["feedback"] = feedback.get(item["project_id"], []) if event["feedback_visible"] or (user and user["role"] in {"organizer", "admin"}) else []
    return {
        "published": bool(event["results_published"]), "event": row_dict(event),
        "awards": awards, "feedback_visible": bool(event["feedback_visible"]), **result,
    }


@app.get("/api/organizer/awards")
def list_awards(
    event_id: int | None = None,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        selected_event = event_id or connection.execute("SELECT id FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        items = rows_dict(connection.execute(
            """SELECT ra.*, p.title AS project_title, pr.amount
               FROM result_awards ra
               LEFT JOIN projects p ON p.id = ra.project_id
               LEFT JOIN prizes pr ON pr.id = ra.prize_id
               WHERE ra.event_id = ? ORDER BY ra.id""",
            (selected_event,),
        ).fetchall())
    return {"event_id": selected_event, "items": items}


@app.post("/api/organizer/awards")
def create_award(
    body: AwardBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT id FROM events WHERE id = ?", (body.event_id,)).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        if body.project_id is not None and not connection.execute(
            "SELECT 1 FROM projects WHERE id = ? AND event_id = ?", (body.project_id, body.event_id)
        ).fetchone():
            raise HTTPException(status_code=400, detail="Award project must belong to the selected event")
        if body.prize_id is not None and not connection.execute(
            "SELECT 1 FROM prizes WHERE id = ? AND event_id = ?", (body.prize_id, body.event_id)
        ).fetchone():
            raise HTTPException(status_code=400, detail="Prize must belong to the selected event")
        try:
            if body.prize_id is not None:
                connection.execute(
                    """INSERT INTO result_awards
                       (event_id, prize_id, project_id, title, recipient_name, created_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(event_id, prize_id) DO UPDATE SET
                         project_id = excluded.project_id, title = excluded.title,
                         recipient_name = excluded.recipient_name""",
                    (body.event_id, body.prize_id, body.project_id, body.title.strip(),
                     body.recipient_name.strip(), user["id"], now()),
                )
            else:
                connection.execute(
                    """INSERT INTO result_awards
                       (event_id, prize_id, project_id, title, recipient_name, created_by, created_at)
                       VALUES (?, NULL, ?, ?, ?, ?, ?)
                       ON CONFLICT(event_id, title) DO UPDATE SET
                         project_id = excluded.project_id, recipient_name = excluded.recipient_name""",
                    (body.event_id, body.project_id, body.title.strip(),
                     body.recipient_name.strip(), user["id"], now()),
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="An award with this title already exists") from exc
        audit(connection, user["id"], "award", "event", str(body.event_id), body.model_dump())
        item = connection.execute(
            "SELECT * FROM result_awards WHERE event_id = ? AND title = ?",
            (body.event_id, body.title.strip()),
        ).fetchone()
    return {"award": row_dict(item)}


@app.delete("/api/organizer/awards/{award_id}")
def delete_award(
    award_id: int,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, bool]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM result_awards WHERE id = ?", (award_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Award not found")
        connection.execute("DELETE FROM result_awards WHERE id = ?", (award_id,))
        audit(connection, user["id"], "delete", "award", str(award_id))
    return {"ok": True}


@app.get("/api/judge/pairwise")
def next_pairwise_comparison(
    event_id: int | None = None,
    user: sqlite3.Row = Depends(role_required("judge")),
) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute(
            "SELECT * FROM events WHERE id = ?",
            (event_id or connection.execute("SELECT id FROM events ORDER BY id DESC LIMIT 1").fetchone()[0],),
        ).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        projects = connection.execute(
            """SELECT p.id, p.title, p.summary, p.team_id, p.track_id, t.name AS track_name,
                      t.color AS track_color, te.name AS team_name
               FROM projects p JOIN assignments a ON a.project_id = p.id AND a.judge_id = ?
               JOIN teams te ON te.id = p.team_id JOIN tracks t ON t.id = p.track_id
               WHERE p.event_id = ? AND p.status = 'submitted' ORDER BY p.id""",
            (user["id"], event["id"]),
        ).fetchall()
        compared = {
            tuple(sorted((row["project_a_id"], row["project_b_id"])))
            for row in connection.execute(
                "SELECT project_a_id, project_b_id FROM pairwise_comparisons WHERE event_id = ? AND judge_id = ?",
                (event["id"], user["id"]),
            ).fetchall()
        }
        candidates = [
            (left, right)
            for index, left in enumerate(projects)
            for right in projects[index + 1:]
            if left["team_id"] != right["team_id"]
            and tuple(sorted((left["id"], right["id"]))) not in compared
            and judge_can_review_track(connection, event["id"], user["id"], left["track_id"])
            and judge_can_review_track(connection, event["id"], user["id"], right["track_id"])
        ]
        if not candidates:
            return {"event": row_dict(event), "pair": None, "completed": len(compared), "remaining": 0}
        candidates.sort(key=lambda pair: hashlib.sha256(
            f"{event['ballot_seed']}:{user['id']}:{pair[0]['id']}:{pair[1]['id']}".encode()
        ).hexdigest())
        left, right = candidates[0]
    return {"event": row_dict(event), "pair": {"left": row_dict(left), "right": row_dict(right)}, "completed": len(compared), "remaining": len(candidates)}


@app.post("/api/judge/pairwise")
def record_pairwise_comparison(
    body: PairwiseComparisonBody,
    user: sqlite3.Row = Depends(role_required("judge")),
) -> dict[str, Any]:
    if body.project_a_id == body.project_b_id or body.winner_project_id not in {body.project_a_id, body.project_b_id}:
        raise HTTPException(status_code=400, detail="Choose one winner from two different projects")
    with db() as connection:
        projects = connection.execute(
            "SELECT id, event_id, team_id, track_id FROM projects WHERE id IN (?, ?)",
            (body.project_a_id, body.project_b_id),
        ).fetchall()
        if len(projects) != 2 or any(project["event_id"] != body.event_id for project in projects):
            raise HTTPException(status_code=404, detail="Both projects must belong to the selected event")
        if projects[0]["team_id"] == projects[1]["team_id"]:
            raise HTTPException(status_code=400, detail="Projects from the same team cannot be compared")
        for project in projects:
            if not connection.execute(
                "SELECT 1 FROM assignments WHERE project_id = ? AND judge_id = ?",
                (project["id"], user["id"]),
            ).fetchone():
                raise HTTPException(status_code=403, detail="Both projects must be assigned to you")
            if not judge_can_review_track(connection, body.event_id, user["id"], project["track_id"]):
                raise HTTPException(status_code=403, detail="Project is outside your authorized tracks")
        first, second = sorted((body.project_a_id, body.project_b_id))
        try:
            connection.execute(
                """INSERT INTO pairwise_comparisons
                   (event_id, judge_id, project_a_id, project_b_id, winner_project_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (body.event_id, user["id"], first, second, body.winner_project_id, now()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="This pair has already been compared") from exc
        audit(connection, user["id"], "compare", "pairwise", str(body.winner_project_id), {
            "event_id": body.event_id, "project_a_id": first, "project_b_id": second,
        })
    return {"ok": True, "winner_project_id": body.winner_project_id}


@app.get("/api/organizer/pairwise")
def organizer_pairwise(
    event_id: int | None = None,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        selected_event = event_id or connection.execute("SELECT id FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        rows = connection.execute(
            """SELECT p.id AS project_id, p.title, te.name AS team_name,
                      sum(CASE WHEN pc.winner_project_id = p.id THEN 1 ELSE 0 END) AS wins,
                      count(pc.id) AS comparisons
               FROM projects p JOIN teams te ON te.id = p.team_id
               LEFT JOIN pairwise_comparisons pc
                 ON pc.event_id = p.event_id AND (pc.project_a_id = p.id OR pc.project_b_id = p.id)
               WHERE p.event_id = ?
               GROUP BY p.id ORDER BY wins DESC, p.title""",
            (selected_event,),
        ).fetchall()
        comparisons = connection.execute(
            """SELECT project_a_id, project_b_id, winner_project_id
               FROM pairwise_comparisons WHERE event_id = ?""",
            (selected_event,),
        ).fetchall()

    items = rows_dict(rows)
    project_ids = [item["project_id"] for item in items]
    strengths = {project_id: 1.0 for project_id in project_ids}
    wins = {project_id: 0 for project_id in project_ids}
    pair_counts: dict[tuple[int, int], int] = {}
    for comparison in comparisons:
        winner = comparison["winner_project_id"]
        left, right = sorted((comparison["project_a_id"], comparison["project_b_id"]))
        if winner not in strengths:
            continue
        wins[winner] += 1
        pair_counts[(left, right)] = pair_counts.get((left, right), 0) + 1

    # Bradley–Terry maximum-likelihood fit using the standard MM update.
    # It is deterministic, bounded, and remains useful when the event has
    # only a small or incomplete set of pairwise comparisons.
    for _ in range(80):
        updated: dict[int, float] = {}
        for project_id in project_ids:
            denominator = 0.0
            for (left, right), count in pair_counts.items():
                if project_id not in (left, right):
                    continue
                other = right if project_id == left else left
                denominator += count / max(1e-9, strengths[project_id] + strengths[other])
            updated[project_id] = wins[project_id] / denominator if denominator else strengths[project_id]
        scale = max(updated.values(), default=1.0) or 1.0
        strengths = {project_id: max(1e-6, value / scale) for project_id, value in updated.items()}

    bt_order = sorted(project_ids, key=lambda project_id: (-strengths[project_id], project_id))
    bt_ranks = {project_id: index + 1 for index, project_id in enumerate(bt_order)}
    for item in items:
        item["bt_score"] = round(strengths[item["project_id"]], 6)
        item["bt_rank"] = bt_ranks[item["project_id"]]
    items.sort(key=lambda item: (item["bt_rank"], item["title"].lower()))
    return {
        "event_id": selected_event,
        "method": "Bradley–Terry maximum-likelihood MM fit",
        "comparison_count": len(comparisons),
        "items": items,
    }


@app.get("/api/judge/projects/{project_id}")
def judge_project(project_id: int, user: sqlite3.Row = Depends(role_required("judge", "organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        project = connection.execute(
            """SELECT p.*, t.name AS track_name, t.color AS track_color, te.name AS team_name FROM projects p
               JOIN tracks t ON t.id = p.track_id JOIN teams te ON te.id = p.team_id WHERE p.id = ?""",
            (project_id,),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] != "submitted" and user["role"] == "judge":
            raise HTTPException(status_code=403, detail="This project is not available for judging")
        assignment = connection.execute("SELECT * FROM assignments WHERE project_id = ? AND judge_id = ?", (project_id, user["id"])).fetchone()
        if user["role"] == "judge" and not assignment:
            audit(connection, user["id"], "denied", "project", str(project_id))
            raise HTTPException(status_code=403, detail="This project is outside your assignment")
        if user["role"] == "judge" and not judge_can_review_track(connection, project["event_id"], user["id"], project["track_id"]):
            audit(connection, user["id"], "denied", "track", str(project["track_id"]), {"project_id": project_id})
            raise HTTPException(status_code=403, detail="This project is outside your authorized tracks")
        criteria = rows_dict(connection.execute("SELECT * FROM criteria WHERE event_id = ? ORDER BY sort_order", (project["event_id"],)).fetchall())
        scores = rows_dict(connection.execute(
            "SELECT s.* FROM scores s JOIN assignments a ON a.id = s.assignment_id WHERE a.project_id = ? AND a.judge_id = ?",
            (project_id, user["id"]),
        ).fetchall())
    return {"project": row_dict(project), "criteria": criteria, "scores": scores, "assignment_id": assignment["id"] if assignment else None}


@app.get("/api/judge/scores")
def judge_scores(
    judge: str | None = None,
    user: sqlite3.Row = Depends(role_required("judge")),
) -> dict[str, Any]:
    """Return only the authenticated judge's scorecards.

    The optional selector exists for the DOGFOOD peer-isolation probe. A judge
    may not use it to retrieve another judge's scores.
    """
    with db() as connection:
        requested_id = user["id"]
        if judge:
            if judge == "judge_a":
                requested = connection.execute(
                    "SELECT id FROM users WHERE role = 'judge' ORDER BY id LIMIT 1"
                ).fetchone()
            elif judge == "judge_b":
                requested = connection.execute(
                    "SELECT id FROM users WHERE role = 'judge' ORDER BY id LIMIT 1 OFFSET 1"
                ).fetchone()
            elif judge.isdigit():
                requested = {"id": int(judge)}
            else:
                requested = connection.execute(
                    "SELECT id FROM users WHERE role = 'judge' AND (email = ? OR name = ?)",
                    (judge, judge),
                ).fetchone()
            if not requested or requested["id"] != user["id"]:
                audit(connection, user["id"], "denied", "judge_scores", judge)
                raise HTTPException(status_code=403, detail="Judges can only view their own scores")

        rows = connection.execute(
            """SELECT s.id, s.score, s.note, s.updated_at, p.id AS project_id,
                      p.title AS project_title, c.id AS criterion_id, c.name AS criterion,
                      c.weight
               FROM scores s
               JOIN assignments a ON a.id = s.assignment_id
               JOIN projects p ON p.id = a.project_id
               JOIN criteria c ON c.id = s.criterion_id
               WHERE a.judge_id = ?
               ORDER BY p.title, c.sort_order""",
            (requested_id,),
        ).fetchall()
    return {"judge": public_user(user), "items": rows_dict(rows)}


@app.post("/api/judge/projects/{project_id}/scores")
def save_scores(project_id: int, body: ScoreBody, user: sqlite3.Row = Depends(role_required("judge", "organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        project = connection.execute(
            "SELECT id, event_id, status FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if user["role"] == "judge":
            if project["status"] != "submitted":
                raise HTTPException(status_code=403, detail="This project is not available for judging")
            event = connection.execute(
                "SELECT * FROM events WHERE id = ?", (project["event_id"],)
            ).fetchone()
            if event and event["judging_opens_at"] and datetime.now(timezone.utc) < parse_optional_time(event["judging_opens_at"]):
                raise HTTPException(status_code=403, detail="Judging has not opened yet")
            if event and event["judging_closes_at"] and datetime.now(timezone.utc) > parse_optional_time(event["judging_closes_at"]):
                raise HTTPException(status_code=403, detail="Judging has closed")
        assignment = connection.execute("SELECT * FROM assignments WHERE project_id = ? AND judge_id = ?", (project_id, user["id"])).fetchone()
        if user["role"] == "judge" and not assignment:
            raise HTTPException(status_code=403, detail="This project is outside your assignment")
        if not assignment:
            assignment = connection.execute("SELECT * FROM assignments WHERE project_id = ? LIMIT 1", (project_id,)).fetchone()
        if not assignment:
            raise HTTPException(status_code=404, detail="Assignment not found")
        criteria = connection.execute(
            "SELECT id, minimum_score, maximum_score FROM criteria WHERE event_id = (SELECT event_id FROM projects WHERE id = ?)",
            (project_id,),
        ).fetchall()
        valid_ids = {str(row["id"]) for row in criteria}
        if set(body.scores) != valid_ids:
            raise HTTPException(status_code=400, detail="A score is required for every rubric criterion")
        ranges = {str(row["id"]): (row["minimum_score"], row["maximum_score"]) for row in criteria}
        if any(
            score < ranges[criterion_id][0] or score > ranges[criterion_id][1]
            for criterion_id, score in body.scores.items()
        ):
            raise HTTPException(status_code=400, detail="One or more scores are outside the criterion's allowed range")
        for criterion_id, score in body.scores.items():
            connection.execute(
                """INSERT INTO scores (assignment_id, criterion_id, score, note, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(assignment_id, criterion_id) DO UPDATE SET score = excluded.score, note = excluded.note, updated_at = excluded.updated_at""",
                (assignment["id"], int(criterion_id), score, body.note, now()),
            )
        audit(connection, user["id"], "score", "project", str(project_id), {"criteria": len(body.scores)})
        project = connection.execute("SELECT event_id, title FROM projects WHERE id = ?", (project_id,)).fetchone()
        dispatch_webhook(connection, "scorecard.submitted", {
            "event_id": project["event_id"],
            "project_id": project_id,
            "project_title": project["title"],
            "judge_id": user["id"],
            "criteria_scored": len(body.scores),
        })
    return {"ok": True}


@app.get("/api/organizer/overview")
def organizer_overview(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
        if not event:
            return {"event": None, "counts": {}, "recent": [], "criteria": []}
        counts = {
            "projects": connection.execute("SELECT COUNT(*) FROM projects WHERE event_id = ?", (event["id"],)).fetchone()[0],
            "submitted": connection.execute("SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'submitted'", (event["id"],)).fetchone()[0],
            "participants": connection.execute(
                """SELECT COUNT(DISTINCT tm.user_id) FROM team_members tm
                   JOIN teams te ON te.id = tm.team_id WHERE te.event_id = ?""",
                (event["id"],),
            ).fetchone()[0],
            "teams": connection.execute("SELECT COUNT(*) FROM teams WHERE event_id = ?", (event["id"],)).fetchone()[0],
            "judges": connection.execute("SELECT COUNT(DISTINCT a.judge_id) FROM assignments a JOIN projects p ON p.id = a.project_id WHERE p.event_id = ?", (event["id"],)).fetchone()[0],
            "assignments": connection.execute("SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id = a.project_id WHERE p.event_id = ?", (event["id"],)).fetchone()[0],
            "scores": connection.execute("SELECT COUNT(*) FROM scores s JOIN assignments a ON a.id = s.assignment_id JOIN projects p ON p.id = a.project_id WHERE p.event_id = ?", (event["id"],)).fetchone()[0],
            "criteria": connection.execute("SELECT COUNT(*) FROM criteria WHERE event_id = ?", (event["id"],)).fetchone()[0],
            "votes": connection.execute("SELECT COUNT(*) FROM votes WHERE event_id = ?", (event["id"],)).fetchone()[0],
        }
        judge_progress = rows_dict(connection.execute(
            """SELECT u.id AS judge_id, u.name AS judge_name, u.email,
                      COUNT(DISTINCT a.id) AS assignments,
                      COUNT(DISTINCT CASE WHEN scored.score_count = criteria_count.criteria_count THEN a.id END) AS completed,
                      COUNT(DISTINCT CASE WHEN COALESCE(scored.score_count, 0) > 0
                                           AND scored.score_count < criteria_count.criteria_count THEN a.id END) AS in_progress
               FROM users u
               JOIN assignments a ON a.judge_id = u.id
               JOIN projects p ON p.id = a.project_id
               LEFT JOIN (
                 SELECT a2.id AS assignment_id, COUNT(s.id) AS score_count
                 FROM assignments a2 LEFT JOIN scores s ON s.assignment_id = a2.id
                 GROUP BY a2.id
               ) scored ON scored.assignment_id = a.id
               CROSS JOIN (
                 SELECT COUNT(*) AS criteria_count FROM criteria WHERE event_id = ?
               ) criteria_count
               WHERE p.event_id = ?
               GROUP BY u.id, u.name, u.email
               ORDER BY u.name""",
            (event["id"], event["id"]),
        ).fetchall())
        recent = rows_dict(connection.execute(
            """SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
               ORDER BY a.created_at DESC LIMIT 8""",
        ).fetchall())
        criteria = rows_dict(connection.execute("SELECT * FROM criteria WHERE event_id = ? ORDER BY sort_order", (event["id"],)).fetchall())
    return {
        "event": row_dict(event), "counts": counts, "recent": recent,
        "criteria": criteria, "judge_progress": judge_progress,
    }


@app.get("/api/organizer/audit-logs")
def organizer_audit_logs(
    q: str = "",
    action: str = "",
    entity: str = "",
    limit: int = 100,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    filters: list[str] = []
    params: list[Any] = []
    if q:
        filters.append("(lower(a.action) LIKE ? OR lower(a.entity) LIKE ? OR lower(a.entity_id) LIKE ? OR lower(coalesce(u.name, '')) LIKE ?)")
        params.extend([f"%{q.lower()}%"] * 4)
    if action:
        filters.append("a.action = ?")
        params.append(action)
    if entity:
        filters.append("a.entity = ?")
        params.append(entity)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with db() as connection:
        rows = connection.execute(
            f"""SELECT a.id, a.action, a.entity, a.entity_id, a.metadata, a.created_at,
                       coalesce(u.name, 'Visitor') AS user_name, u.email
                FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
                {where} ORDER BY a.created_at DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
    items = rows_dict(rows)
    for item in items:
        item["metadata"] = decode_json_field(item["metadata"], {})
    return {"items": items}


@app.get("/api/admin/users")
def admin_users(user: sqlite3.Row = Depends(role_required("admin"))) -> dict[str, Any]:
    with db() as connection:
        items = rows_dict(connection.execute(
            "SELECT id, email, name, role, created_at FROM users ORDER BY name, id"
        ).fetchall())
    return {"items": items}


@app.patch("/api/admin/users/{user_id}/role")
def update_user_role(
    user_id: int,
    body: UserRoleBody,
    user: sqlite3.Row = Depends(role_required("admin")),
) -> dict[str, Any]:
    with db() as connection:
        target = connection.execute("SELECT id, email, name, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["id"] == user["id"] and body.role != "admin":
            raise HTTPException(status_code=400, detail="An admin cannot remove their own admin access")
        connection.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        audit(connection, user["id"], "role_change", "user", str(user_id), {"before": target["role"], "after": body.role})
        updated = connection.execute("SELECT id, email, name, role FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"user": row_dict(updated)}


@app.get("/api/organizer/export/scores.csv")
def export_scores(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT p.title, te.name AS team, tr.name AS track, u.name AS judge, c.name AS criterion,
                      c.weight, s.score, s.note, s.updated_at
               FROM scores s JOIN assignments a ON a.id = s.assignment_id JOIN projects p ON p.id = a.project_id
               JOIN teams te ON te.id = p.team_id JOIN tracks tr ON tr.id = p.track_id JOIN users u ON u.id = a.judge_id
               JOIN criteria c ON c.id = s.criterion_id ORDER BY p.title, u.name, c.sort_order""",
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["project", "team", "track", "judge", "criterion", "weight", "score", "note", "updated_at"])
    writer.writerows([tuple(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-scores.csv"})


@app.get("/api/organizer/export/normalized.csv")
def export_normalized_scores(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    result = organizer_results(event_id=None, user=user)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["normalized_rank", "raw_rank", "project", "team", "judges", "raw_score", "normalized_score", "rank_delta"])
    for item in result["items"]:
        writer.writerow([
            item["normalized_rank"], item["raw_rank"], item["title"], item["team_name"],
            ", ".join(item["judges"]), item["raw_score"], item["normalized_score"], item["rank_delta"],
        ])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hacknight-normalized-scores.csv"},
    )


@app.get("/api/organizer/export/participants.csv")
def export_participants(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT u.name, u.email, u.role, e.name AS event_name, te.name AS team_name,
                      tm.member_role
               FROM users u
               LEFT JOIN team_members tm ON tm.user_id = u.id
               LEFT JOIN teams te ON te.id = tm.team_id
               LEFT JOIN events e ON e.id = te.event_id
               ORDER BY u.name"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "email", "role", "event", "team", "team_role"])
    writer.writerows([tuple(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-participants.csv"})


@app.get("/api/organizer/export/teams.csv")
def export_teams(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT e.name AS event_name, te.name AS team_name, te.invite_code,
                      count(tm.user_id) AS member_count
               FROM teams te JOIN events e ON e.id = te.event_id
               LEFT JOIN team_members tm ON tm.team_id = te.id
               GROUP BY te.id ORDER BY e.name, te.name"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["event", "team", "invite_code", "member_count"])
    writer.writerows([tuple(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-teams.csv"})


@app.get("/api/organizer/export/votes.csv")
def export_votes(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT e.name AS event_name, p.title AS project, v.quantity, v.credits_spent,
                      v.voter_key, v.created_at
               FROM votes v JOIN projects p ON p.id = v.project_id
               JOIN events e ON e.id = v.event_id ORDER BY v.created_at DESC"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["event", "project", "quantity", "credits_spent", "voter_key", "created_at"])
    writer.writerows([tuple(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-votes.csv"})


@app.get("/api/organizer/export/judges.csv")
def export_judges(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT u.name, u.email, e.name AS event_name, t.name AS authorized_track
               FROM users u
               LEFT JOIN judge_tracks jt ON jt.judge_id = u.id
               LEFT JOIN events e ON e.id = jt.event_id
               LEFT JOIN tracks t ON t.id = jt.track_id
               WHERE u.role = 'judge' ORDER BY u.name, e.name, t.name"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["judge", "email", "event", "authorized_track"])
    writer.writerows([tuple(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-judges.csv"})


@app.get("/api/organizer/export/assignments.csv")
def export_assignments(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT e.name AS event_name, p.title AS project, te.name AS team,
                      u.name AS judge, p.status, a.id AS assignment_id,
                      COALESCE(scored.score_count, 0) AS score_count,
                      (SELECT COUNT(*) FROM criteria c WHERE c.event_id = p.event_id) AS criteria_count
               FROM assignments a JOIN projects p ON p.id = a.project_id
               JOIN events e ON e.id = p.event_id JOIN teams te ON te.id = p.team_id
               JOIN users u ON u.id = a.judge_id
               LEFT JOIN (SELECT assignment_id, COUNT(*) AS score_count FROM scores GROUP BY assignment_id)
                 scored ON scored.assignment_id = a.id
               ORDER BY e.name, p.title, u.name"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["event", "project", "team", "judge", "status", "assignment_id", "scores_recorded", "criteria_required"])
    writer.writerows([tuple(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-assignments.csv"})


@app.get("/api/organizer/export/results.csv")
def export_results(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    result = organizer_results(event_id=None, user=user)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["normalized_rank", "raw_rank", "project", "team", "raw_score", "normalized_score", "rank_delta", "judges"])
    for item in result["items"]:
        writer.writerow([
            item["normalized_rank"], item["raw_rank"], item["title"], item["team_name"],
            item["raw_score"], item["normalized_score"], item["rank_delta"], ", ".join(item["judges"]),
        ])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-results.csv"})


@app.get("/api/organizer/export/audit.csv")
def export_audit(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT a.created_at, coalesce(u.name, 'Visitor') AS user_name, a.action,
                      a.entity, a.entity_id, a.metadata
               FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
               ORDER BY a.created_at DESC"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["created_at", "user", "action", "entity", "entity_id", "metadata"])
    writer.writerows([tuple(row) for row in rows])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=hacknight-audit.csv"})


@app.get("/api/export.csv")
def dogfood_export(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    """Compatibility alias declared in .dogfood.toml."""
    return export_scores(user=user)


@app.get("/api/organizer/export/projects.csv")
def export_projects(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> StreamingResponse:
    with db() as connection:
        rows = connection.execute(
            """SELECT p.title, p.summary, p.repo_url, p.demo_url, p.status, p.submitted_at,
                      te.name AS team_name, tr.name AS track_name, e.slug AS event_slug
               FROM projects p
               JOIN teams te ON te.id = p.team_id
               JOIN tracks tr ON tr.id = p.track_id
               JOIN events e ON e.id = p.event_id
               ORDER BY e.id, p.title"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["event_slug", "team_name", "track_name", "title", "summary", "repo_url", "demo_url", "status", "submitted_at"])
    writer.writerows([
        (row["event_slug"], row["team_name"], row["track_name"], row["title"], row["summary"],
         row["repo_url"], row["demo_url"], row["status"], row["submitted_at"])
        for row in rows
    ])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hacknight-projects.csv"},
    )


@app.get("/api/organizer/export/event.json")
def export_event(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="No event found")
        event_id = event["id"]
        payload = {
            "event": row_dict(event),
            "tracks": rows_dict(connection.execute("SELECT * FROM tracks WHERE event_id = ?", (event_id,)).fetchall()),
            "prizes": rows_dict(connection.execute("SELECT * FROM prizes WHERE event_id = ?", (event_id,)).fetchall()),
            "criteria": rows_dict(connection.execute("SELECT * FROM criteria WHERE event_id = ?", (event_id,)).fetchall()),
            "teams": rows_dict(connection.execute("SELECT * FROM teams WHERE event_id = ?", (event_id,)).fetchall()),
            "projects": rows_dict(connection.execute("SELECT * FROM projects WHERE event_id = ?", (event_id,)).fetchall()),
            "assignments": rows_dict(connection.execute(
                """SELECT a.* FROM assignments a JOIN projects p ON p.id = a.project_id
                   WHERE p.event_id = ?""", (event_id,)
            ).fetchall()),
            "scores": rows_dict(connection.execute(
                """SELECT s.* FROM scores s JOIN assignments a ON a.id = s.assignment_id
                   JOIN projects p ON p.id = a.project_id WHERE p.event_id = ?""", (event_id,)
            ).fetchall()),
        }
        audit(connection, user["id"], "export", "event", str(event_id), {"format": "json"})
    return payload


@app.post("/api/organizer/import/event")
def import_event_data(
    body: ImportEventBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    """Restore an exported event configuration into an existing event.

    Entity IDs are deliberately not trusted across databases. Configuration is
    matched by names and the category import endpoints remain the safe path for
    participants, teams, projects, and judges.
    """
    source_event = body.payload.get("event") or {}
    with db() as connection:
        current = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Event not found")
        fields = {
            key: source_event[key]
            for key in (
                "name", "tagline", "description", "starts_at", "deadline",
                "registration_opens_at", "registration_closes_at", "hackathon_ends_at",
                "judging_opens_at", "judging_closes_at", "results_publish_at", "status",
                "voting_access", "voting_mode", "voting_opens_at", "voting_closes_at",
                "results_visible", "feedback_visible", "quadratic_budget",
                "team_min_size", "team_max_size", "comments_enabled",
            )
            if key in source_event
        }
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            connection.execute(
                f"UPDATE events SET {assignments} WHERE id = ?",
                (*fields.values(), body.event_id),
            )
        track_ids: dict[str, int] = {}
        for track in body.payload.get("tracks", []):
            name = str(track.get("name", "")).strip()
            if not name:
                continue
            existing = connection.execute(
                "SELECT id FROM tracks WHERE event_id = ? AND lower(name) = lower(?)",
                (body.event_id, name),
            ).fetchone()
            if existing:
                track_ids[name.lower()] = existing["id"]
                connection.execute(
                    "UPDATE tracks SET color = ? WHERE id = ?",
                    (track.get("color", "#ff3b86"), existing["id"]),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO tracks (event_id, name, color) VALUES (?, ?, ?)",
                    (body.event_id, name, track.get("color", "#ff3b86")),
                )
                track_ids[name.lower()] = cursor.lastrowid
        for prize in body.payload.get("prizes", []):
            title = str(prize.get("title", "")).strip()
            if title:
                existing = connection.execute(
                    "SELECT id FROM prizes WHERE event_id = ? AND lower(title) = lower(?)",
                    (body.event_id, title),
                ).fetchone()
                if existing:
                    connection.execute(
                        "UPDATE prizes SET amount = ? WHERE id = ?",
                        (prize.get("amount", ""), existing["id"]),
                    )
                else:
                    connection.execute(
                        "INSERT INTO prizes (event_id, title, amount) VALUES (?, ?, ?)",
                        (body.event_id, title, prize.get("amount", "")),
                    )
        audit(connection, user["id"], "import", "event", str(body.event_id), {
            "tracks": len(track_ids), "prizes": len(body.payload.get("prizes", [])),
            "source_event": source_event.get("name", ""),
        })
    return {
        "event_id": body.event_id,
        "tracks_imported": len(track_ids),
        "prizes_imported": len(body.payload.get("prizes", [])),
        "note": "Use the participant, team, project, and judge import endpoints for the related records.",
    }


@app.post("/api/organizer/import/projects")
def import_projects(body: ImportProjectsBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        imported = 0
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate(body.rows, start=1):
            title = str(item.get("title", "")).strip()
            summary = str(item.get("summary", item.get("description", ""))).strip()
            team_name = str(item.get("team_name", item.get("team", ""))).strip() or f"Imported team {index}"
            track_name = str(item.get("track_name", item.get("track", ""))).strip() or "General"
            if len(title) < 2 or len(summary) < 10:
                rejected.append({"row": index, "error": "title and summary are required"})
                continue
            track = connection.execute(
                "SELECT * FROM tracks WHERE event_id = ? AND lower(name) = lower(?)",
                (body.event_id, track_name),
            ).fetchone()
            if not track:
                color = ["#ff3b86", "#25e0c2", "#a78bfa"][index % 3]
                track_cursor = connection.execute(
                    "INSERT INTO tracks (event_id, name, color) VALUES (?, ?, ?)",
                    (body.event_id, track_name, color),
                )
                track = connection.execute("SELECT * FROM tracks WHERE id = ?", (track_cursor.lastrowid,)).fetchone()
            team = connection.execute(
                "SELECT * FROM teams WHERE event_id = ? AND lower(name) = lower(?)",
                (body.event_id, team_name),
            ).fetchone()
            if not team:
                team_cursor = connection.execute(
                    "INSERT INTO teams (event_id, name, invite_code, created_at) VALUES (?, ?, ?, ?)",
                    (body.event_id, team_name, secrets.token_urlsafe(8), now()),
                )
                team = connection.execute("SELECT * FROM teams WHERE id = ?", (team_cursor.lastrowid,)).fetchone()
            slug = "-".join(title.lower().split()) + "-" + secrets.token_hex(3)
            status = "submitted" if str(item.get("status", "submitted")).lower() == "submitted" else "draft"
            submitted_at = now() if status == "submitted" else None
            project_cursor = connection.execute(
                """INSERT INTO projects
                   (event_id, team_id, track_id, title, slug, summary, repo_url, demo_url, status, submitted_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    body.event_id, team["id"], track["id"], title, slug, summary,
                    str(item.get("repo_url", item.get("repository_url", ""))).strip(),
                    str(item.get("demo_url", item.get("project_url", ""))).strip(),
                    status, submitted_at, now(),
                ),
            )
            imported += 1
            dispatch_webhook(connection, "project.imported", {"event_id": body.event_id, "project_id": project_cursor.lastrowid})
        audit(connection, user["id"], "import", "project", str(body.event_id), {"imported": imported, "rejected": len(rejected)})
    return {"imported": imported, "rejected": rejected}


@app.post("/api/organizer/import/participants")
def import_participants(body: ImportRowsBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM events WHERE id = ?", (body.event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        imported = 0
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate(body.rows, start=1):
            name = str(item.get("name", "")).strip()
            email = str(item.get("email", "")).strip().lower()
            team_name = str(item.get("team_name", "")).strip()
            if len(name) < 2 or "@" not in email:
                rejected.append({"row": index, "error": "name and a valid email are required"})
                continue
            existing = connection.execute("SELECT id FROM users WHERE lower(email) = ?", (email,)).fetchone()
            if existing:
                user_id = existing["id"]
            else:
                cursor = connection.execute(
                    "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, 'participant', ?, ?)",
                    (email, name, hash_password(str(item.get("password", "participant"))), now()),
                )
                user_id = cursor.lastrowid
            if team_name:
                team = connection.execute(
                    "SELECT id FROM teams WHERE event_id = ? AND lower(name) = lower(?)",
                    (body.event_id, team_name),
                ).fetchone()
                if not team:
                    cursor = connection.execute(
                        "INSERT INTO teams (event_id, name, invite_code, created_at) VALUES (?, ?, ?, ?)",
                        (body.event_id, team_name, secrets.token_urlsafe(8), now()),
                    )
                    team_id = cursor.lastrowid
                else:
                    team_id = team["id"]
                if not connection.execute(
                    "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                    (team_id, user_id),
                ).fetchone():
                    connection.execute(
                        "INSERT INTO team_members (team_id, user_id, member_role) VALUES (?, ?, 'member')",
                        (team_id, user_id),
                    )
            imported += 1
        audit(connection, user["id"], "import", "participant", str(body.event_id), {"imported": imported, "rejected": len(rejected)})
    return {"imported": imported, "rejected": rejected}


@app.post("/api/organizer/import/teams")
def import_teams(body: ImportRowsBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM events WHERE id = ?", (body.event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        imported = 0
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate(body.rows, start=1):
            name = str(item.get("name", item.get("team_name", ""))).strip()
            if len(name) < 2:
                rejected.append({"row": index, "error": "team name is required"})
                continue
            if connection.execute(
                "SELECT 1 FROM teams WHERE event_id = ? AND lower(name) = lower(?)",
                (body.event_id, name),
            ).fetchone():
                rejected.append({"row": index, "error": "team already exists"})
                continue
            connection.execute(
                "INSERT INTO teams (event_id, name, invite_code, created_at) VALUES (?, ?, ?, ?)",
                (body.event_id, name, secrets.token_urlsafe(8), now()),
            )
            imported += 1
        audit(connection, user["id"], "import", "team", str(body.event_id), {"imported": imported, "rejected": len(rejected)})
    return {"imported": imported, "rejected": rejected}


@app.post("/api/organizer/import/judges")
def import_judges(body: ImportRowsBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM events WHERE id = ?", (body.event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        imported = 0
        rejected: list[dict[str, Any]] = []
        for index, item in enumerate(body.rows, start=1):
            name = str(item.get("name", "")).strip()
            email = str(item.get("email", "")).strip().lower()
            if len(name) < 2 or "@" not in email:
                rejected.append({"row": index, "error": "name and a valid email are required"})
                continue
            if connection.execute("SELECT 1 FROM users WHERE lower(email) = ?", (email,)).fetchone():
                rejected.append({"row": index, "error": "user email already exists"})
                continue
            cursor = connection.execute(
                "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, 'judge', ?, ?)",
                (email, name, hash_password(str(item.get("password", "judge"))), now()),
            )
            judge_id = cursor.lastrowid
            track_names = item.get("tracks", [])
            for track_name in track_names if isinstance(track_names, list) else []:
                track = connection.execute(
                    "SELECT id FROM tracks WHERE event_id = ? AND lower(name) = lower(?)",
                    (body.event_id, str(track_name).strip()),
                ).fetchone()
                if track:
                    connection.execute(
                        "INSERT OR IGNORE INTO judge_tracks (event_id, judge_id, track_id) VALUES (?, ?, ?)",
                        (body.event_id, judge_id, track["id"]),
                    )
            imported += 1
        audit(connection, user["id"], "import", "judge", str(body.event_id), {"imported": imported, "rejected": len(rejected)})
    return {"imported": imported, "rejected": rejected}


@app.get("/api/organizer/webhooks")
def list_webhooks(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        items = rows_dict(connection.execute(
            """SELECT w.*, e.name AS event_name,
                      (SELECT COUNT(*) FROM webhook_deliveries d WHERE d.webhook_id = w.id) AS delivery_count
               FROM webhooks w LEFT JOIN events e ON e.id = w.event_id ORDER BY w.created_at DESC"""
        ).fetchall())
    for item in items:
        item["secret"] = f"{item['secret'][:4]}…" if item["secret"] else ""
    return {"items": items}


@app.post("/api/organizer/webhooks")
def create_webhook(body: WebhookBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Webhook URL must start with http:// or https://")
    with db() as connection:
        if body.event_id is not None and not connection.execute("SELECT 1 FROM events WHERE id = ?", (body.event_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Event not found")
        hook_secret = body.secret or secrets.token_urlsafe(24)
        cursor = connection.execute(
            """INSERT INTO webhooks (event_id, url, secret, events, active, created_by, created_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (body.event_id, body.url, hook_secret, ",".join(body.events or ["*"]), user["id"], now()),
        )
        audit(connection, user["id"], "create", "webhook", str(cursor.lastrowid), {"events": body.events})
        hook = connection.execute("SELECT * FROM webhooks WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"webhook": row_dict(hook), "secret": hook_secret}


@app.get("/api/organizer/webhooks/{webhook_id}/deliveries")
def webhook_deliveries(webhook_id: int, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM webhooks WHERE id = ?", (webhook_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Webhook not found")
        items = rows_dict(connection.execute(
            "SELECT id, webhook_id, event_type, status, response_code, error, attempt_count, created_at, delivered_at FROM webhook_deliveries WHERE webhook_id = ? ORDER BY created_at DESC LIMIT 100",
            (webhook_id,),
        ).fetchall())
    return {"items": items}


@app.post("/api/organizer/certificates")
def issue_certificate(body: CertificateBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        project = connection.execute(
            """SELECT p.*, e.name AS event_name, te.name AS team_name
               FROM projects p JOIN events e ON e.id = p.event_id JOIN teams te ON te.id = p.team_id
               WHERE p.id = ? AND p.status = 'submitted'""",
            (body.project_id,),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Submitted project not found")
        existing = connection.execute("SELECT * FROM certificates WHERE project_id = ?", (body.project_id,)).fetchone()
        if existing:
            return {"certificate": row_dict(existing)}
        code = secrets.token_urlsafe(12)
        cursor = connection.execute(
            """INSERT INTO certificates
               (event_id, project_id, recipient_name, award_title, certificate_code, issued_by, issued_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (project["event_id"], body.project_id, body.recipient_name, body.award_title, code, user["id"], now()),
        )
        audit(connection, user["id"], "issue", "certificate", str(cursor.lastrowid), {"project_id": body.project_id})
        dispatch_webhook(connection, "certificate.issued", {"event_id": project["event_id"], "project_id": body.project_id, "certificate_code": code})
        certificate = connection.execute("SELECT * FROM certificates WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"certificate": row_dict(certificate)}


@app.get("/api/organizer/certificates")
def list_certificates(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        items = rows_dict(connection.execute(
            """SELECT c.*, p.title AS project_title, e.name AS event_name
               FROM certificates c JOIN projects p ON p.id = c.project_id JOIN events e ON e.id = c.event_id
               ORDER BY c.issued_at DESC"""
        ).fetchall())
    return {"items": items}


@app.get("/api/organizer/judges")
def list_judges(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        items = rows_dict(connection.execute("SELECT id, name, email FROM users WHERE role = 'judge' ORDER BY name").fetchall())
    return {"items": items}


@app.post("/api/organizer/judge-records")
def issue_judge_record(body: JudgeRecordBody, user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        event = connection.execute("SELECT * FROM events WHERE id = ?", (body.event_id,)).fetchone()
        judge = connection.execute("SELECT id, name, role FROM users WHERE id = ? AND role = 'judge'", (body.judge_id,)).fetchone()
        if not event or not judge:
            raise HTTPException(status_code=404, detail="Event or judge not found")
        assignments = connection.execute(
            """SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id = a.project_id
               WHERE a.judge_id = ? AND p.event_id = ?""",
            (body.judge_id, body.event_id),
        ).fetchone()[0]
        scorecards = connection.execute(
            """SELECT COUNT(DISTINCT a.id) FROM assignments a JOIN projects p ON p.id = a.project_id
               JOIN scores s ON s.assignment_id = a.id
               WHERE a.judge_id = ? AND p.event_id = ?""",
            (body.judge_id, body.event_id),
        ).fetchone()[0]
        issued_at = now()
        payload = {
            "type": "judge_participation",
            "event_id": body.event_id,
            "event_name": event["name"],
            "judge_id": body.judge_id,
            "judge_name": judge["name"],
            "assignments_completed": assignments,
            "scorecards_started": scorecards,
            "issued_at": issued_at,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        signature = hmac.new(SESSION_SECRET.encode(), serialized.encode(), hashlib.sha256).hexdigest()
        code = secrets.token_urlsafe(12)
        cursor = connection.execute(
            """INSERT INTO judge_records
               (event_id, judge_id, record_code, payload, signature, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (body.event_id, body.judge_id, code, serialized, signature, issued_at),
        )
        audit(connection, user["id"], "issue", "judge_record", str(cursor.lastrowid), {"judge_id": body.judge_id})
        dispatch_webhook(connection, "judge_record.issued", {"event_id": body.event_id, "judge_id": body.judge_id, "record_code": code})
        record = connection.execute("SELECT * FROM judge_records WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"record": {**payload, "record_code": code, "signature": signature, "verification_url": f"/api/judge-records/{code}"}}


@app.get("/api/organizer/judge-records")
def list_judge_records(user: sqlite3.Row = Depends(role_required("organizer", "admin"))) -> dict[str, Any]:
    with db() as connection:
        items = rows_dict(connection.execute(
            """SELECT r.id, r.event_id, r.judge_id, r.record_code, r.signature, r.created_at,
                      e.name AS event_name, u.name AS judge_name
               FROM judge_records r JOIN events e ON e.id = r.event_id JOIN users u ON u.id = r.judge_id
               ORDER BY r.created_at DESC"""
        ).fetchall())
    return {"items": items}


@app.get("/api/judge-records/{record_code}")
def verify_judge_record(record_code: str) -> dict[str, Any]:
    with db() as connection:
        record = connection.execute("SELECT * FROM judge_records WHERE record_code = ?", (record_code,)).fetchone()
    if not record:
        raise HTTPException(status_code=404, detail="Judge record not found")
    expected = hmac.new(SESSION_SECRET.encode(), record["payload"].encode(), hashlib.sha256).hexdigest()
    valid = hmac.compare_digest(expected, record["signature"])
    payload = json.loads(record["payload"])
    return {"valid": valid, "record": {**payload, "record_code": record_code, "signature": record["signature"]}}


@app.get("/api/certificates/{certificate_code}", response_class=HTMLResponse)
def certificate(certificate_code: str) -> HTMLResponse:
    with db() as connection:
        item = connection.execute(
            """SELECT c.*, p.title AS project_title, te.name AS team_name, e.name AS event_name
               FROM certificates c JOIN projects p ON p.id = c.project_id
               JOIN teams te ON te.id = p.team_id JOIN events e ON e.id = c.event_id
               WHERE c.certificate_code = ?""",
            (certificate_code,),
        ).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Certificate not found")
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{item['award_title']} · {item['event_name']}</title>
<style>body{{font-family:Georgia,serif;background:#0b1022;color:#eef2ff;display:grid;place-items:center;min-height:100vh;margin:0}}
.certificate{{width:min(760px,85vw);padding:70px;text-align:center;border:1px solid #ff3b86;background:#111936;box-shadow:0 0 0 12px #0b1022,0 0 0 13px #25e0c2}}
h1{{font-size:42px;margin:18px 0;color:#ff78aa}}h2{{font-size:28px;font-weight:400}}p{{color:#aeb8d4;font-family:system-ui}}</style></head>
<body><main class="certificate"><p>HACKNIGHT · CERTIFICATE OF ACHIEVEMENT</p>
<h1>{item['award_title']}</h1><p>This certifies that</p><h2>{item['recipient_name']}</h2>
<p>earned this award for <strong>{item['project_title']}</strong> with {item['team_name']} at {item['event_name']}.</p>
<p>Issued {item['issued_at'][:10]} · {item['certificate_code']}</p></main></body></html>""")


@app.get("/embed/gallery", response_class=HTMLResponse)
def embedded_gallery(event_id: int | None = None) -> HTMLResponse:
    event_query = f"&event_id={event_id}" if event_id else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hacknight gallery</title>
<style>body{{margin:0;background:#0b1022;color:#eef2ff;font:14px system-ui}}main{{padding:18px}}h1{{font-size:18px;margin:0 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}article{{border:1px solid #263153;background:#111936;padding:15px;border-radius:6px}}
h2{{font-size:16px;margin:10px 0 6px}}p{{color:#aeb8d4;line-height:1.45}}small{{color:#25e0c2}}</style></head>
<body><main><h1>Hacknight project gallery</h1><div class="grid" id="projects">Loading…</div></main>
<script>
fetch('/api/gallery?{event_query[1:]}').then(r=>r.json()).then(data=>{{
  document.querySelector('#projects').innerHTML=data.items.map(p=>`<article><small>${{p.track_name}}</small><h2>${{p.title}}</h2><p>${{p.summary}}</p><small>${{p.votes == null ? 'Results hidden' : p.votes + ' community votes'}}</small></article>`).join('')||'<p>No submitted projects yet.</p>';
}}).catch(()=>document.querySelector('#projects').textContent='Gallery unavailable');
</script></body></html>""")


@app.post("/api/votes")
def vote(body: VoteBody, request: Request, user: sqlite3.Row | None = Depends(optional_current_user)) -> dict[str, Any]:
    with db() as connection:
        project = connection.execute(
            "SELECT id, event_id FROM projects WHERE id = ? AND status = 'submitted'",
            (body.project_id,),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Submitted project not found")
        event = connection.execute("SELECT * FROM events WHERE id = ?", (project["event_id"],)).fetchone()
        policy = voting_policy(event, request, user)
        require_voting_window(policy)
        voter_key = policy["voter_key"]
        check_rate_limit(connection, voter_key, "vote", 20, 600)
        if policy["mode"] in {"one_per_project", "one_per_event"} and body.quantity != 1:
            raise HTTPException(status_code=400, detail="This ballot allows one vote at a time")
        if policy["mode"] == "one_per_event":
            existing_event_vote = connection.execute(
                "SELECT 1 FROM votes WHERE event_id = ? AND voter_key = ? LIMIT 1",
                (project["event_id"], voter_key),
            ).fetchone()
            if existing_event_vote:
                raise HTTPException(status_code=409, detail="This ballot allows one vote per event")
        if policy["mode"] == "quadratic":
            current_quantity = connection.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM votes WHERE event_id = ? AND voter_key = ? AND project_id = ?",
                (project["event_id"], voter_key, body.project_id),
            ).fetchone()[0]
            spent = connection.execute(
                "SELECT COALESCE(SUM(credits_spent), 0) FROM votes WHERE event_id = ? AND voter_key = ?",
                (project["event_id"], voter_key),
            ).fetchone()[0]
            new_quantity = current_quantity + body.quantity
            incremental_cost = (new_quantity * new_quantity) - (current_quantity * current_quantity)
            if spent + incremental_cost > policy["quadratic_budget"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Quadratic ballot budget exceeded; {policy['quadratic_budget'] - spent} credits remain",
                )
            existing = connection.execute(
                "SELECT id FROM votes WHERE event_id = ? AND voter_key = ? AND project_id = ?",
                (project["event_id"], voter_key, body.project_id),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE votes SET quantity = ?, credits_spent = credits_spent + ?, created_at = ? WHERE id = ?",
                    (new_quantity, incremental_cost, now(), existing["id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO votes
                       (project_id, user_id, event_id, voter_key, quantity, credits_spent, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (body.project_id, user["id"] if user else None, project["event_id"], voter_key, body.quantity, incremental_cost, now()),
                )
        else:
            try:
                connection.execute(
                    """INSERT INTO votes
                       (project_id, user_id, event_id, voter_key, quantity, credits_spent, created_at)
                       VALUES (?, ?, ?, ?, 1, 1, ?)""",
                    (body.project_id, user["id"] if user else None, project["event_id"], voter_key, now()),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="You already voted on this ballot")
        audit(connection, user["id"] if user else None, "vote", "project", str(body.project_id), {
            "voter": "authenticated" if user else policy["access"],
            "mode": policy["mode"],
            "quantity": body.quantity,
        })
        dispatch_webhook(connection, "vote.created", {
            "event_id": project["event_id"],
            "project_id": body.project_id,
            "mode": policy["mode"],
            "quantity": body.quantity,
        })
        count = connection.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM votes WHERE project_id = ?",
            (body.project_id,),
        ).fetchone()[0]
    return {"votes": count, "quantity": body.quantity, "mode": policy["mode"]}


@app.post("/api/comments")
def comment(body: CommentBody, request: Request, user: sqlite3.Row | None = Depends(lambda session=Cookie(default=None, alias=SESSION_COOKIE): session_user(session))) -> dict[str, Any]:
    author = user["name"] if user else "Community member"
    with db() as connection:
        project = connection.execute(
            """SELECT p.id, p.event_id, e.comments_enabled
               FROM projects p JOIN events e ON e.id = p.event_id
               WHERE p.id = ? AND p.status = 'submitted'""",
            (body.project_id,),
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Submitted project not found")
        if not project["comments_enabled"]:
            raise HTTPException(status_code=403, detail="Comments are disabled for this event")
        bucket = f"user:{user['id']}" if user else f"ip:{request.client.host if request.client else 'anonymous'}"
        check_rate_limit(connection, bucket, "comment", 10, 600)
        cursor = connection.execute(
            "INSERT INTO comments (project_id, author_user_id, author_name, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (body.project_id, user["id"] if user else None, author, body.body.strip(), now()),
        )
        audit(connection, user["id"] if user else None, "comment", "project", str(body.project_id))
        dispatch_webhook(connection, "comment.created", {"event_id": project["event_id"], "project_id": body.project_id})
        item = connection.execute("SELECT * FROM comments WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"comment": row_dict(item)}


@app.get("/api/organizer/comments")
def organizer_comments(
    event_id: int | None = None,
    status: str = "",
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        filters = ["p.event_id = COALESCE(?, p.event_id)"]
        params: list[Any] = [event_id]
        if status:
            filters.append("c.status = ?")
            params.append(status)
        rows = connection.execute(
            f"""SELECT c.*, p.title AS project_title, e.name AS event_name
                FROM comments c JOIN projects p ON p.id = c.project_id
                JOIN events e ON e.id = p.event_id
                WHERE {' AND '.join(filters)}
                ORDER BY CASE WHEN c.status = 'reported' THEN 0 ELSE 1 END, c.created_at DESC""",
            params,
        ).fetchall()
    return {"items": rows_dict(rows)}


@app.delete("/api/comments/{comment_id}")
def delete_comment(
    comment_id: int,
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, bool]:
    with db() as connection:
        item = connection.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Comment not found")
        if user["role"] not in {"organizer", "admin"} and item["author_user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="You can only delete your own comment")
        connection.execute(
            "UPDATE comments SET status = 'removed', moderated_by = ? WHERE id = ?",
            (user["id"], comment_id),
        )
        audit(connection, user["id"], "delete", "comment", str(comment_id))
    return {"ok": True}


@app.post("/api/comments/{comment_id}/report")
def report_comment(
    comment_id: int,
    body: CommentReportBody,
    request: Request,
    user: sqlite3.Row | None = Depends(optional_current_user),
) -> dict[str, bool]:
    with db() as connection:
        item = connection.execute("SELECT id, project_id FROM comments WHERE id = ? AND status = 'active'", (comment_id,)).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Active comment not found")
        bucket = f"user:{user['id']}" if user else f"ip:{request.client.host if request.client else 'anonymous'}"
        check_rate_limit(connection, bucket, "comment_report", 20, 600)
        connection.execute(
            "UPDATE comments SET status = 'reported', report_reason = ?, reported_at = ? WHERE id = ?",
            (body.reason.strip(), now(), comment_id),
        )
        audit(connection, user["id"] if user else None, "report", "comment", str(comment_id), {"reason": body.reason.strip()})
    return {"ok": True}


@app.patch("/api/organizer/comments/{comment_id}")
def moderate_comment(
    comment_id: int,
    body: CommentModerationBody,
    user: sqlite3.Row = Depends(role_required("organizer", "admin")),
) -> dict[str, Any]:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM comments WHERE id = ?", (comment_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Comment not found")
        connection.execute(
            "UPDATE comments SET status = ?, moderated_by = ? WHERE id = ?",
            (body.status, user["id"], comment_id),
        )
        audit(connection, user["id"], "moderate", "comment", str(comment_id), {"status": body.status})
        item = connection.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    return {"comment": row_dict(item)}


init_db()
