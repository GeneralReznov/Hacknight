# Hacknight: Implemented Architecture

This document describes the architecture that is present in the repository,
not the complete wishlist from the original product specification. The
implementation was checked against `app/main.py`, `app/static/`, `README.md`,
`JUDGING.md`, `BONUS-EVIDENCE.md`, and the SQLite schema.

The acceptance receipt currently verifies T1 and T2 behavior. The repository
also contains implemented bonus paths for normalization proof, pairwise
judging, the threat model, and the API/OpenAPI surface.

## 1. Runtime topology

Hacknight is a single-process, local-first application:

```text
Browser
  │
  │ HTTP
  ▼
FastAPI / Uvicorn process
  ├── Serves app/static/index.html, app.js, and styles.css
  ├── JSON API under /api/*
  ├── Public project gallery and result responses
  ├── Printable certificate pages
  └── Embeddable gallery at /embed/gallery
          │
          ▼
      SQLite database
          │
          ▼
   data/hackathon.db or DB_PATH
```

The supported local runtime is:

```bash
docker compose up
```

The application does not require a hosted database, hosted authentication
provider, cloud account, or external API for its core workflows. Optional
webhook delivery can make outbound HTTP requests when an organizer registers
one, but webhook delivery is not required for the application to work.

The FastAPI application is started by `app.main:app`. It mounts the static
directory at `/static` and initializes the SQLite schema and seed data during
startup. `/api/healthz` returns the health status used by local/deployment
checks.

## 2. Implemented application layers

The current implementation is intentionally small, but its responsibilities
are separated logically:

### Browser layer

- `app/static/index.html` provides the application shell.
- `app/static/app.js` renders the login, visitor gallery, overview, results,
  participant submission, judge queue, and organizer desk views.
- `app/static/styles.css` provides the visual system.
- The browser uses the same-origin `/api` prefix and cookie credentials.
- The browser hides controls by role, but backend checks remain authoritative.

### API and application layer

`app/main.py` contains FastAPI routes, Pydantic request models, role
dependencies, event/deadline checks, judging rules, voting rules, audit
writes, imports/exports, certificates, and webhook dispatch.

The route handlers use SQLite connections with foreign keys enabled. Database
context managers commit successful work and roll back failed work. SQL
parameters are used for data values.

### Persistence layer

SQLite is the source of truth. The database is created at `DB_PATH`, which
defaults to `data/hackathon.db`. Foreign-key enforcement is enabled for every
connection. The Docker deployment persists the database through its mounted
data volume.

The application stores image, video, repository, and live-demo references as
URLs. It does not upload media to an object store.

### Optional local integration layer

The following optional capabilities are implemented without becoming
dependencies of the core workflow:

- HMAC-signed webhook notifications and delivery history;
- JSON-based event/category import;
- CSV and event JSON export;
- locally rendered project certificates;
- signed judge participation records with public verification;
- embeddable gallery HTML.

## 3. Authentication and roles

### Implemented roles

The `users.role` column supports:

- `participant`;
- `judge`;
- `organizer`;
- `admin`;
- `visitor`.

Visitors normally use the anonymous visitor mode in the browser rather than a
registered database account. The application also allows unauthenticated
public gallery, project detail, comments, voting, result, certificate, and
verification requests where the event policy permits them.

### Authentication behavior

Implemented authentication endpoints:

```text
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
GET  /api/session
```

Registration creates a participant account and starts a session. Login
verifies the local password hash and starts a session. Logout removes the
session cookie.

Passwords use PBKDF2-HMAC-SHA256 with a per-password salt. Sessions are
HMAC-signed cookie values containing a user id and random session component.
The cookie is HTTP-only, uses SameSite=Lax, and has a 12-hour `max_age`.
`COOKIE_SECURE` can enable the Secure attribute.

`SESSION_SECRET` is read from the environment, with a local development
fallback retained for the DOGFOOD checker. Deployments should provide their
own secret.

Login and registration use SQLite-backed rolling rate-limit buckets. There is
no password-reset endpoint or external email delivery flow in the current
application.

### Backend authorization

`role_required(...)` is applied to protected routes. Resource-specific checks
are performed after the role check:

- participants can mutate only their own team's data;
- judges can read and write only assigned projects and scorecards;
- organizers and admins can operate event administration routes;
- only admins can list users and change user roles;
- organizer/admin routes are not available to participants or judges.

The API returns authorization errors for direct unauthorized requests; access
is not enforced only by hiding browser navigation.

## 4. Event, team, and submission workflows

### Event configuration

Organizers and admins can create and update events with:

- name, tagline, and description;
- event start and submission deadline;
- optional registration open/close times;
- optional hackathon end;
- optional judging open/close times;
- optional results publication time;
- draft, active, or archived stored status;
- tracks and prizes;
- team minimum and maximum sizes;
- custom questions;
- judging criteria;
- comments and voting configuration;
- feedback visibility.

Event timelines are validated for contradictory dates. The API exposes a
derived phase through `event_phase(...)`. Implemented phase values are:

```text
draft
registration_open
registration_closed
hackathon_active
submission_closed
judging_active
results_pending
results_published
archived
```

The exact phase depends on stored dates, stored status, and the result
publication flag. Submission, judging, and voting date checks are performed on
the server.

### Teams

Participants can:

- create one team for an event;
- join a team with its invite code;
- invite an account by email lookup and share the team invite code;
- view team members;
- leave a team.

Team membership is stored in `team_members`. The API enforces the configured
maximum team size and prevents a participant from belonging to multiple teams
for the same event. A captain is transferred to another member when the
captain leaves. An empty team is deleted.

There is no participant invitation table or email-sending service. Team
invitations are implemented through the unique `teams.invite_code` value.
Judge invitations use the separate `judge_invitations` table and token
endpoints.

### Projects and submission states

Each team can have one project per event. The `projects` table stores:

- title and slug;
- summary, tagline, and long description;
- thumbnail URL and JSON image URL list;
- demo video URL;
- JSON technology tag list;
- JSON custom-question answers;
- repository URL and live demo URL;
- event, team, and track relationships;
- status and timestamps.

Implemented project states are:

```text
draft
submitted
locked
```

Participants can create or update their team's project before the server-side
deadline. They can save a draft or submit it. Participants cannot set
`locked`; organizers/admins can set that state through the API. After the
deadline, participant create/update requests are rejected. Each team is
limited to one project for the event.

Custom questions are defined on the event. Unknown custom-answer keys and
missing required answers are rejected.

The application accepts media and external links as user-provided URLs. It
does not fetch or execute submitted repositories, demos, videos, or images on
the server.

## 5. Actual SQLite data model

The schema is created in `init_db()` in `app/main.py`. The implemented tables
are:

```text
users
events
tracks
prizes
result_awards
teams
team_members
projects
criteria
assignments
pairwise_comparisons
scores
votes
comments
custom_questions
judge_tracks
audit_log
judge_invitations
rate_limits
webhooks
webhook_deliveries
certificates
judge_records
```

Important relationships and constraints:

- events own tracks, prizes, criteria, custom questions, teams, and
  event-scoped operational records;
- teams belong to events and connect users through `team_members`;
- projects belong to one event, one team, and one track;
- assignments connect a judge user to a project and are unique per
  judge/project pair;
- scores belong to an assignment and criterion and are unique per
  assignment/criterion pair;
- votes are unique per project/voter key;
- comments belong to projects and carry moderation status;
- result awards belong to events and may reference a prize and project;
- judge records and certificates have unique public verification codes.

The implementation does not have separate `roles`, `reviews`,
`review_scores`, `normalized_scores`, `project_images`, `project_tags`,
`custom_answers`, or `results` tables. Those concepts are represented by
columns, JSON fields, assignment/score rows, derived calculations, event
publication flags, and `result_awards`.

## 6. Judge assignment and scoring

### Judge invitations and track authorization

Organizers can create judge invitations at:

```text
POST /api/organizer/judge-invitations
GET  /api/organizer/judge-invitations
GET  /api/judge-invitations/{token}
POST /api/judge-invitations/{token}/accept
```

An accepted invitation changes the matching participant account to the judge
role. Organizers can assign judges to authorized tracks through
`PUT /api/organizer/judge-tracks`.

If a judge has no track rows, the current implementation treats the judge as
authorized for all tracks. If track rows exist, the judge is restricted to
those tracks.

### Manual and automatic assignment

Manual assignment:

```text
POST /api/organizer/assignments
```

Automatic assignment:

```text
POST /api/organizer/assignments/auto
```

The automatic endpoint accepts `reviews_per_project`, balances by current
assignment count, skips existing assignments, skips a judge's own team, and
skips unauthorized tracks. It returns project ids that could not reach the
requested review count.

The database uniqueness constraint and route logic prevent duplicate
judge/project assignments. Only submitted projects can be assigned.

### Judge queue and scorecards

```text
GET  /api/judge/assignments
GET  /api/judge/projects/{project_id}
GET  /api/judge/scores
POST /api/judge/projects/{project_id}/scores
```

The judge queue returns only the authenticated judge's assignments and a
summary of assigned, completed, remaining, and percentage-complete work. A
judge project request checks both assignment membership and authorized track.
The score endpoint prevents access to another judge's scores.

The scorecard stores one score per configured criterion and one written note
on each criterion row. A judge must provide every criterion before a scorecard
is accepted. Score values are checked against the criterion's configured
range, with the current schema and UI limited to a 1–5 scale. Judge scoring
also checks the configured judging open/close times.

### Weighted score and normalization implementation

The current result calculation uses the stored 1–5 scale:

```text
raw_contribution = score × criterion_weight / 5
raw_project_score = average(weighted contributions across judges)
```

Normalization is calculated when the organizer results endpoint is requested;
normalized values are not persisted in a `normalized_scores` table. For each
judge's recorded criterion scores:

```text
mean   = average(scores)
stddev = population standard deviation(scores)
spread = stddev, or 1 when stddev is zero
normalized = clamp(50 + 10 × ((raw - mean) / spread), 0, 100)
```

Normalized criterion contributions are weighted and averaged across judges.
The organizer results response includes:

- raw and normalized project scores;
- judge score count, mean, and standard deviation;
- raw-to-normalized criterion conversions;
- raw rank and normalized rank;
- rank movement;
- raw and normalized contribution values;
- the formula and method description.

The same proof is rendered in the organizer desk. The method is documented
in [`JUDGING.md`](JUDGING.md). Normalization does not modify raw score rows.

## 7. Optional pairwise judging

Pairwise judging is implemented separately from the official weighted
scorecard:

```text
GET  /api/judge/pairwise
POST /api/judge/pairwise
GET  /api/organizer/pairwise
```

The judge receives a deterministic pair from their assigned submitted
projects. The API rejects:

- identical projects;
- projects from the same team;
- projects from different events;
- projects not assigned to the judge;
- projects outside authorized tracks;
- duplicate comparisons;
- a winner that is not one of the two projects.

The organizer endpoint calculates a bounded, deterministic Bradley–Terry
maximum-likelihood estimate using an MM update and returns comparison counts,
wins, strength, and rank. Pairwise results do not replace the weighted
scorecard ranking.

## 8. Public gallery, voting, and comments

### Gallery

Implemented public surfaces:

```text
GET /api/gallery
GET /api/projects/{project_id}
GET /embed/gallery
```

The gallery returns submitted projects for the selected/latest event. It
supports server-side text search and track filtering, while the browser also
filters technology tags. The project detail view returns the project fields,
images, external links, active comments, and policy-controlled vote counts.
The browser randomizes the gallery ordering through the API query.

Judge averages and community vote counts are hidden according to the current
event publication/voting policy. Draft projects are not public.

### Voting

Organizers can configure:

- `open`, `email`, or `authenticated` access;
- `one_per_project`;
- `one_per_event`;
- `quadratic` voting;
- voting start and end times;
- whether results are visible during an active ballot;
- the quadratic budget.

Voting is implemented at:

```text
POST /api/votes
```

The voter key is derived from the authenticated user, a supplied local voter
key, an email header for email-gated mode, or an IP fallback for open mode.
The route enforces the voting window, duplicate detection, configured ballot
mode, quadratic budget, and a rolling rate limit. Votes are audited and
organizer export is available.

Email-gated mode checks the supplied email value; the application does not
send or verify an email challenge.

### Comments and moderation

Implemented endpoints:

```text
POST   /api/comments
DELETE /api/comments/{comment_id}
POST   /api/comments/{comment_id}/report
GET    /api/organizer/comments
PATCH  /api/organizer/comments/{comment_id}
```

Anonymous and authenticated users can post comments when the event enables
comments. Authenticated users can delete their own comments. Organizers and
admins can moderate comments as active, hidden, or removed. Anyone can report
an active comment. Comment creation and reporting are rate-limited and
audited.

## 9. Organizer, admin, and results operations

### Organizer overview

```text
GET /api/organizer/overview
GET /api/organizer/audit-logs
```

The overview returns counts for projects, submitted projects, participants,
teams, assigned judges, assignments, scores, criteria, and votes. It also
returns judge progress and recent audit activity.

The organizer desk implements:

- event creation;
- voting policy configuration;
- feedback release configuration;
- rubric editing with weights required to total 100%;
- manual and automatic assignment;
- normalization proof;
- pairwise ranking;
- result awards and special awards;
- comment moderation;
- judge invitations;
- project import;
- webhooks;
- certificates;
- judge records;
- audit search/filter;
- export links.

### User administration

Admins can list users and change a user's role:

```text
GET   /api/admin/users
PATCH /api/admin/users/{user_id}/role
```

The API prevents an admin from removing their own final admin access.

### Results and feedback

Organizers/admins can publish and unpublish result visibility:

```text
GET  /api/organizer/results
POST /api/organizer/results/publish
POST /api/organizer/results/unpublish
GET  /api/results
```

Before publication, public results return no ranking items. After publication,
the public result response includes normalized rank and score, project/team
information, awards, and feedback only when `feedback_visible` is enabled.

Awards are stored in `result_awards`. Organizers can create, update through
upsert behavior, list, and delete prize-linked or special awards.

## 10. Audit, imports, and exports

### Audit log

The `audit_log` table stores:

```text
user_id
action
entity
entity_id
metadata JSON text
created_at
```

The application audits authentication, team/project changes, assignments,
scores, pairwise comparisons, votes, comments, moderation, publishing,
awards, role changes, imports, exports, certificates, judge records,
webhooks, and selected denied-access paths.

The organizer endpoint supports text, action, entity, and bounded-limit
filtering. The UI provides client-side search over the returned rows.

The current audit schema does not store dedicated before/after columns or
dedicated IP/session columns. Where additional context is recorded, it is
stored in the JSON metadata field.

### Imports

Imports use JSON request bodies rather than uploaded CSV files:

```text
POST /api/organizer/import/event
POST /api/organizer/import/projects
POST /api/organizer/import/participants
POST /api/organizer/import/teams
POST /api/organizer/import/judges
```

Project imports are bounded to 500 rows. Participant, team, and judge imports
are bounded to 1,000 rows. Invalid rows return row-level rejection details.
Project imports can create missing teams and tracks. Event imports restore
event configuration, tracks, and prizes into an existing event; the response
directs related entity restoration to the category-specific import endpoints.

### Exports

Implemented organizer exports include:

```text
/api/organizer/export/scores.csv
/api/organizer/export/normalized.csv
/api/organizer/export/participants.csv
/api/organizer/export/teams.csv
/api/organizer/export/projects.csv
/api/organizer/export/judges.csv
/api/organizer/export/assignments.csv
/api/organizer/export/votes.csv
/api/organizer/export/results.csv
/api/organizer/export/audit.csv
/api/organizer/export/event.json
/api/export.csv                 # DOGFOOD compatibility alias
```

`event.json` is an event snapshot containing the selected event, tracks,
prizes, criteria, teams, projects, assignments, and scores. It is not a full
database dump: votes, comments, certificates, judge records, webhook
deliveries, pairwise comparisons, and audit history are not included in that
payload.

## 11. Certificates, judge records, and webhooks

### Certificates

Organizers/admins can issue one printable certificate per submitted project:

```text
POST /api/organizer/certificates
GET  /api/organizer/certificates
GET  /api/certificates/{certificate_code}
```

The certificate is rendered locally as HTML and has a unique public code.
Certificate issuance is audited and emits the optional webhook event.

This is a project certificate flow. The application does not generate a
separate participant certificate batch or PDF file.

### Judge participation records

Organizers/admins can issue signed judge participation records:

```text
POST /api/organizer/judge-records
GET  /api/organizer/judge-records
GET  /api/judge-records/{record_code}
```

The record payload includes event, judge, assignment, scorecard, and issue
information. The payload is HMAC-signed with `SESSION_SECRET`, stored with a
unique verification code, and publicly verifiable.

### Webhooks

Organizers/admins can register event-specific or global webhook subscriptions:

```text
GET  /api/organizer/webhooks
POST /api/organizer/webhooks
GET  /api/organizer/webhooks/{webhook_id}/deliveries
```

The application stores a pending delivery before attempting a POST. Delivery
uses a two-second timeout and an `X-Hacknight-Signature` HMAC header. Status,
response code, error, attempt count, and timestamps are stored. A failed
delivery remains visible and does not roll back the core action.

Webhook destinations are organizer-supplied URLs. The current implementation
does not provide a separate SSRF allowlist or outbound network policy; this is
documented as a threat-model limitation.

## 12. Implemented security controls and limits

Implemented controls include:

- backend role checks on protected routes;
- participant ownership and event/team checks;
- judge assignment and track checks;
- server-side submission and judging deadline checks;
- PBKDF2-HMAC-SHA256 password hashing;
- HMAC-signed HTTP-only session cookies;
- SQLite foreign keys and uniqueness constraints;
- Pydantic request validation;
- bounded import row counts;
- login, registration, voting, comment, and report rate limits;
- duplicate vote and duplicate assignment protection;
- public hiding of drafts, unpublished results, and private judge scores;
- audit records for sensitive mutations and selected denied requests;
- HMAC-signed webhook requests with bounded delivery time;
- HTML escaping in the browser for displayed user content.

Known operational limits are intentional:

- SQLite is a single-process local-first store and is not a horizontally
  scaled judging service.
- Public-link voting uses voter keys, duplicate checks, rate limits, and audit
  data but cannot stop determined Sybil voters or VPN rotation.
- Local audit data is not an immutable external ledger.
- Webhook URLs are organizer-controlled network destinations.
- Submitted external links are untrusted; the server does not run submitted
  code.
- There is no password-reset email flow, media upload service, or external
  identity provider.

The detailed security analysis is in [`threat_model.md`](threat_model.md).

## 13. API and compatibility surface

FastAPI generates the live API documentation at:

```text
GET /docs
GET /openapi.json
```

The main implemented API groups are:

```text
/api/auth/*
/api/session
/api/events
/api/gallery
/api/projects/*
/api/me/*
/api/teams/*
/api/judge/*
/api/judge-invitations/*
/api/organizer/*
/api/results
/api/votes
/api/comments/*
/api/certificates/*
/api/judge-records/*
/api/admin/*
/api/healthz
```

`/projects`, `/projects/new`, and `/api/export.csv` are DOGFOOD compatibility
aliases. They use the normal application checks where they perform a
meaningful action.

## 14. Verification evidence

The repository contains these implementation references:

- `acceptance-report.txt` records passing T1/T2 checks for public gallery,
  submission deadline enforcement, judge self-access, peer-score isolation,
  participant blocking, and CSV export.
- `JUDGING.md` documents assignment isolation, the weighted rubric,
  normalization proof, pairwise judging, and operational integrity behavior.
- `BONUS-EVIDENCE.md` maps normalization proof, pairwise judging, threat model,
  and API-first/OpenAPI evidence to implemented routes.
- `threat_model.md` records the actual trust boundaries, required guarantees,
  and known limitations.

This architecture intentionally excludes claims for capabilities that are
specified but not present in the current code, including password reset,
participant email invitations, uploaded media storage, a full database
restore/import, persisted normalized-score rows, separate review/result
tables, and dedicated audit before/after or IP/session columns.