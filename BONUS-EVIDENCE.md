# Bonus Evidence

This document maps the optional DOGFOOD bonus challenges to the implemented
features, source documents, and live API evidence.

The four documented bonuses are worth **+16 tie-breaker points total**:

| Bonus | Value | Evidence |
| --- | ---: | --- |
| Normalization proof | +5 | `JUDGING.md`, organizer results API, Organizer Desk |
| Pairwise judging | +5 | `/api/judge/pairwise`, `/api/organizer/pairwise`, Judge Queue |
| Threat model | +3 | `threat_model.md` |
| API-first design | +3 | `/docs`, `/openapi.json`, this document |

These bonuses break ties; they do not change the four-criterion weighted score.

## Normalization proof

The organizer results endpoint returns:

- Raw judge score profiles
- Per-judge score count, mean, and standard deviation
- Every raw-to-normalized criterion conversion
- Raw and normalized weighted contributions
- Raw rank, normalized rank, and rank movement

The method is:

```text
normalized = clamp(50 + 10 × ((raw - judge_mean) / judge_stddev), 0, 100)
```

The implementation uses a population standard deviation and substitutes a
spread of `1` when a judge has no variation. The original raw score remains
unchanged. The normalized values are used only for organizer ranking and
publication.

Live evidence:

```text
GET /api/organizer/results
```

Look for `normalization.judge_profiles` and
`normalization.ranking_changes`. The same evidence is rendered in Organizer
Desk under `RAW → NORMALIZED → RANKED`.

## Pairwise judging

Assigned judges receive deterministic head-to-head comparisons through:

```text
GET  /api/judge/pairwise
POST /api/judge/pairwise
GET  /api/organizer/pairwise
```

The API enforces that:

- Both projects belong to the selected event
- Both projects are assigned to the authenticated judge
- Both projects are inside the judge's authorized tracks
- The projects belong to different teams
- A pair cannot be submitted twice

The organizer endpoint calculates a deterministic Bradley–Terry
maximum-likelihood estimate using a bounded MM update and returns `bt_score`
and `bt_rank`. Pairwise comparisons remain separate from the official weighted
rubric.

## Threat model

`threat_model.md` documents:

- Assets
- Browser/API, role, database, and webhook trust boundaries
- STRIDE-inspired spoofing, tampering, repudiation, disclosure, denial of
  service, and privilege escalation analysis
- Required security guarantees
- Explicit limits around Sybil voting, local audit storage, SQLite scale, and
  organizer-controlled webhook destinations

## API-first design

FastAPI generates the API specification from the live route definitions:

```text
GET /docs
GET /openapi.json
```

The major UI capabilities have corresponding API routes:

| Capability | API surface |
| --- | --- |
| Authentication and session | `/api/auth/*`, `/api/session` |
| Event configuration | `/api/events/*`, `/api/organizer/events/*` |
| Teams and invites | `/api/teams/*`, `/api/me/team` |
| Projects and gallery | `/api/projects/*`, `/api/gallery` |
| Judge assignments and scores | `/api/judge/*`, `/api/organizer/assignments*` |
| Results and publication | `/api/organizer/results*`, `/api/results` |
| Voting and comments | `/api/votes`, `/api/comments*` |
| Awards and feedback | `/api/organizer/awards*`, `/api/organizer/events/*/feedback` |
| Webhooks | `/api/organizer/webhooks*` |
| Certificates and judge records | `/api/organizer/certificates`, `/api/organizer/judge-records`, verification routes |
| Imports and exports | `/api/organizer/import/*`, `/api/organizer/export/*` |
| Moderation and administration | `/api/organizer/comments*`, `/api/admin/*` |

The API is the authorization boundary. The frontend does not provide the
security guarantee by itself; direct API requests receive the same role,
ownership, assignment, deadline, and publication checks.

## Verification commands

Run the syntax and static checks:

```bash
python3 -m py_compile app/main.py
node --check app/static/app.js
git diff --check
```

Run the shared acceptance suite against the local seeded service:

```bash
python3 run.py .dogfood.toml
```

The acceptance suite verifies public gallery access, shared fixture content,
deadline rejection, judge self-access, peer-score isolation, participant
blocking, and organizer CSV export.