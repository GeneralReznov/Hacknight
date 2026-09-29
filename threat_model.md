# Threat Model

## Project Overview

Hacknight is a self-hosted hackathon submission, judging, voting, and results
platform. A single FastAPI process serves the vanilla JavaScript frontend,
JSON API, embeddable gallery, and a local SQLite database. It is designed to
run offline with `docker compose up`.

Users have visitor, participant, judge, organizer, or admin roles. Participants
form teams and submit projects. Organizers configure events, assign judges,
review normalized results, publish awards and feedback, and export event data.
Judges score only their assignments. Visitors can browse projects and, when
configured, vote and comment.

## Assets

- **Credentials and sessions** — local email/password credentials, password
  hashes, and signed HTTP-only session cookies. Compromise allows impersonation
  of a participant, judge, organizer, or admin.
- **Participant and judge identity data** — names, email addresses, team
  membership, invitations, and judge records. This is personal data and also
  controls access to event resources.
- **Projects and submitted links** — project descriptions, repository URLs,
  demo URLs, images, videos, custom answers, and private draft content.
  Submitted links are untrusted user content.
- **Judging records** — assignments, rubric scores, private notes, normalized
  results, pairwise comparisons, and audit history. Unauthorized disclosure or
  tampering can change the perceived outcome of an event.
- **Voting and moderation data** — voter keys, votes, comments, reports, and
  rate-limit buckets. Abuse can distort community awards or expose commenters.
- **Organizer capabilities and event configuration** — schedules, tracks,
  prizes, publication flags, webhooks, certificates, and imports/exports.
  These are high-impact administrative actions.
- **Application secrets** — the session signing secret and webhook signing
  secrets. They must never be returned in ordinary API responses or committed
  to source control.

## Trust Boundaries

- **Browser to FastAPI** — the browser is untrusted. Every protected API
  request must authenticate and authorize independently of frontend navigation.
- **Public to authenticated** — gallery, project detail, public results, and
  selected voting/comment routes cross this boundary. Private scores, drafts,
  team management, and organizer operations remain protected.
- **Participant to team-owned data** — a participant may manage only their
  event team and its project. Team, event, track, and deadline constraints are
  server-side rules.
- **Judge to assigned judging data** — a judge may read and write only assigned
  projects, criteria, and scorecards, including any configured track boundary.
- **Organizer/admin to event administration** — event configuration, scoring
  policy, publication, awards, imports, exports, webhooks, and moderation
  require organizer or admin authorization. User role changes require admin.
- **FastAPI to SQLite** — the application has direct database access. Queries
  must remain parameterized, foreign keys enabled, and sensitive mutations
  auditable.
- **FastAPI to webhook destinations** — organizer-supplied HTTP endpoints
  receive event payloads. Delivery is optional, bounded by a short timeout,
  HMAC-signed, and recorded. Network access to a webhook is still a server-side
  SSRF and data-exfiltration surface.
- **Local development to deployment** — the fallback session secret and seeded
  demo passwords are for local DOGFOOD compatibility only. Deployments must
  provide a private session secret and persistent protected storage.

## Scan Anchors

- Production entry point: `app/main.py`; static client: `app/static/`.
- Highest-risk areas: session validation, role dependencies, project/judge
  authorization, voting/comment handlers, webhook delivery, import handlers,
  and result publication.
- Public surfaces: `/`, `/projects`, `/api/gallery`,
  `/api/projects/{project_id}`, `/api/results`, `/embed/gallery`, and public
  verification pages.
- Authenticated surfaces: `/api/me/*`, `/api/judge/*`, `/api/votes`, and
  `/api/comments`.
- Organizer/admin surfaces: `/api/organizer/*` and `/api/admin/*`.
- Dev/demo data: `fixtures.json`, `.dogfood.toml`, and the temporary SQLite
  database used by the preview workflow.

## Threat Categories

### Spoofing

An attacker who obtains a valid session cookie can act as that user, and a
weak deployment secret would allow forged cookies. The application must verify
the HMAC signature and user record for every protected request. Sessions must
use a deployment-provided secret in production, and passwords must remain
hashed rather than logged or returned. Local seeded credentials are demo
credentials, not production credentials.

### Tampering

Participants must not move projects between events or teams, submit after the
deadline, or modify another team's project. Judges must not write scores for
unassigned projects, unauthorized tracks, incomplete rubrics, or out-of-range
criteria. Organizers must be the only role able to change judging weights,
publication flags, awards, imports, exports, and moderation state.

All such rules must be checked in the API using the authenticated user and
database relationships. Client-provided IDs, scores, event IDs, award IDs, and
publication flags must be validated against the database before mutation.

### Repudiation

Sensitive actions must have an audit record containing the acting user, action,
entity, entity ID, metadata, and timestamp. This includes authentication,
denied judge access, project and score changes, votes, comments, exports,
publication, moderation, awards, pairwise comparisons, certificates, judge
records, and webhook registration.

Audit logs are an accountability record within the local SQLite deployment.
Operators must protect the database file and include it in the backup policy;
the application does not claim tamper-proof external log storage.

### Information Disclosure

Judge scores and notes must remain private until the organizer publishes the
appropriate public result or feedback view. A judge must receive a 403 when
requesting another judge's scorecard or an unassigned project. Participants
must not see another team's private project data.

Public project and result responses must exclude private scorecard details
unless the event's release policy permits them. Secrets, password hashes,
session signatures, and webhook secrets must not appear in client responses or
logs. Submitted repository, demo, image, and video URLs are untrusted external
content and must not be executed by the server.

### Denial of Service

Authentication, voting, commenting, registration, and other public mutation
surfaces must use bounded request bodies and rate limits where appropriate.
Webhook delivery must have a short timeout and a failure must not block the
core event workflow. Imports must have bounded row counts and return
row-level rejections rather than running unbounded work.

The local SQLite deployment is intentionally single-process and does not claim
to resist a determined network-level volumetric attack. Operators should place
it behind a suitable reverse proxy for public events.

### Elevation of Privilege

Role checks must be applied server-side through route dependencies. Object IDs
are not authorization: project, assignment, team, event, award, certificate,
import, and moderation routes must verify ownership or organizer scope after
authentication.

SQL queries must remain parameterized. Imports must not trust foreign database
IDs as proof of ownership. User role changes must be restricted to admins, and
organizer operations must not be reachable by participants or judges.

## Required Guarantees

- Every protected API endpoint MUST validate a signed session and role.
- Judge project, score, and pairwise routes MUST enforce assignment and track
  boundaries in the backend.
- Participant project and team mutations MUST enforce event ownership,
  membership, one-project-per-team, and deadline rules.
- Published public results MUST not expose unreleased judge scores or notes.
- All score values MUST be validated against the configured rubric and every
  required criterion MUST be present before completion.
- All database writes MUST use parameterized SQL and preserve foreign-key
  relationships.
- Sensitive mutations and denied access MUST be written to the audit log.
- Webhook requests MUST include an HMAC signature and a short delivery
  timeout; failed delivery MUST remain visible without breaking the event.
- Importers MUST bound input size, validate references, and report rejected
  rows instead of silently dropping them.
- Production deployments MUST set a strong private `SESSION_SECRET`, protect
  SQLite storage, and replace seeded demo credentials.

## Known Limits

- This is a local-first SQLite application, not a horizontally scaled judging
  service.
- Public-link voting can reduce casual abuse with voter keys, duplicate
  detection, rate limits, and audit records, but it cannot stop determined
  Sybil voters or VPN rotation.
- HMAC proves a webhook came from the application to a recipient that knows
  the secret; it does not make an organizer-provided destination trustworthy.
- Audit records are locally stored and operationally protected, not an
  immutable external ledger.