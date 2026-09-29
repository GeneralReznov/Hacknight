# Hacknight

Hacknight is a self-hosted hackathon submission and judging platform. It serves FastAPI backend from one process, with SQLite persistence and the shared fixture data.

## Run it

```bash
docker compose up
```

Open [http://localhost:8080](http://localhost:8080).

The app has no hosted database, cloud account, external auth provider, or runtime API dependency. The first container build needs the Python packages from `requirements.txt`; after the image exists, the application itself is fully local.

### Deploy to Render

This repository includes `docker-compose.yaml` for a Docker-based web service. Create a new Render Blueprint from the repository; Render will generate `SESSION_SECRET`, mount a persistent disk at `/app/data`, and use `/api/healthz` for health checks. SQLite persistence requires a persistent disk and a single web instance. The same Docker image also works on other container hosts that provide a persistent volume.

### Seeded accounts

| Role | Email | Password |
| --- | --- | --- |
| Organizer | organizer@hacknight.local | organizer |
| Fixture judge | tomas.varga@example.org | judge |
| Fixture judge | wei.lindqvist@example.org | judge |
| Fixture participant | ada40@example.org | participant |

The organizer account is used for local administration: `organizer@hacknight.local` / `organizer`. The fixture contains 30 judge accounts and participant accounts for team members; all fixture judges use `judge` and all fixture participants use `participant`. Visitors can browse without an account. New participants can create an account from the sign-in screen; registration creates a participant role and starts a signed session immediately.

## What works

- Local authentication and signed HTTP-only sessions
- Participant registration with rate limiting and secure password hashing
- Visitor, participant, judge, organizer, and admin role model
- Event creation with dates, tracks, and prizes
- Team membership and seeded teams
- Draft and submitted project states
- Deadline enforcement in the API
- Searchable public gallery
- Judge assignment queue and backend-enforced assignment isolation
- Judge invitations, batch assignment from the organizer desk, and team invite links
- Weighted, organizer-editable three-criterion rubric with weights enforced to 100%
- Private scorecards, cross-judge normalization proof, ranking movement, and organizer CSV export
- Authenticated community votes with duplicate detection and rolling rate limits
- Project comments with rolling rate limits and public project detail views
- Signed, best-effort webhooks with delivery history
- Printable local certificates with public verification pages
- Bulk project import plus project CSV and full event JSON export
- Embeddable gallery at `/embed/gallery`
- Audit log for authentication, project, score, event, vote, comment, export, and denied-access activity

## API

The API is documented by the running FastAPI OpenAPI document at `/docs` and `/openapi.json`. The most important endpoints are:

- `POST /api/auth/login`, `GET /api/session`
- `GET /api/events`, `POST /api/events`
- `GET /api/gallery`, `GET /api/projects/{id}`
- `GET /projects`, `POST /projects/new` (DOGFOOD compatibility aliases)
- `POST /api/projects`, `PUT /api/projects/{id}`
- `GET /api/judge/assignments`
- `GET /api/judge/scores` (authenticated judge's scorecards only)
- `GET /api/judge/projects/{id}`, `POST /api/judge/projects/{id}/scores`
- `GET /api/organizer/overview`, `GET /api/organizer/export/scores.csv`
- `GET /api/export.csv` (DOGFOOD compatibility alias)
- `GET /api/organizer/export/projects.csv`, `GET /api/organizer/export/event.json`
- `GET /api/organizer/export/{scores,normalized,participants,teams,votes,judges,assignments,results,audit}.csv`
- `POST /api/organizer/import/{event,projects,participants,teams,judges}`
- `POST /api/organizer/webhooks`, `GET /api/organizer/webhooks/{id}/deliveries`
- `POST /api/organizer/certificates`, `GET /api/certificates/{code}`
- `GET /api/judge/pairwise`, `POST /api/judge/pairwise`, `GET /api/organizer/pairwise`
- `GET /api/organizer/results`, `POST /api/organizer/results/{publish,unpublish}`
- `GET /api/organizer/judge-records`, `POST /api/organizer/judge-records`
- `GET /api/organizer/comments`, `PATCH /api/organizer/comments/{id}`
- `GET /embed/gallery`

The optional bonus evidence is collected in
[`BONUS-EVIDENCE.md`](BONUS-EVIDENCE.md), and the security reference is in
[`threat_model.md`](threat_model.md).

## Scoring coverage

The platform is designed around the organisers' offline acceptance tiers:

- T1: authentication, roles, events, teams, deadline-enforced submissions, and gallery
- T2: assignments, organizer-weighted rubric, backend role isolation, normalization, audit trail, and CSV export
- T3: authenticated voting, duplicate detection, rate limiting, comments, and public project detail
- T4: REST/OpenAPI, signed webhooks, certificates, bulk import/export, and an embeddable gallery

## Optional bonus coverage

- Normalization proof: documented in `JUDGING.md` and returned by the organizer results API
- Pairwise judging: assigned-judge comparisons with a Bradley–Terry ranking
- Threat model: `threat_model.md`
- API-first design: generated OpenAPI at `/docs` and `/openapi.json`

## DOGFOOD 2026

The repository includes `fixtures.json`, `.dogfood.toml`, and the checker receipt in `acceptance-report.txt`.
On a fresh database, startup loads the shared event, 8 tracks, 40 teams, 41 projects, 30 judges,
and all supplied scorecards. Run the checker with:

```bash
python3 run.py .dogfood.toml
```

Pairwise judging is available as an optional bonus workflow for assigned judges. It is recorded separately from the weighted rubric, so organizers can compare the head-to-head signal with the normalized ranking without changing the official scorecard result.
