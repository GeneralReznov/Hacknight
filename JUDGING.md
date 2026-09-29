# Judging integrity

## Assignment isolation

Assignments are explicit rows connecting a judge to a project. The judge endpoints query by both `project_id` and the authenticated `judge_id`. If the pair does not exist, the API returns `403` and records a denied-access audit event. This is enforced in the backend, not just in the frontend.

## Weighted rubric

Each criterion is scored from 1 to 5. The shared fixture uses this seeded rubric:

| Criterion | Weight |
| --- | ---: |
| Functionality & completeness | 50% |
| Quality & operability | 30% |
| Innovation & clarity | 20% |

The weighted score for a project is:

```text
weighted_score = Σ((criterion_score / 5) × criterion_weight)
```

The organizer results endpoint stores and exposes both raw weighted scores and normalized scores. Normalization is an organizer-only view and does not change the underlying raw score record or assignment boundary.

Organizers can edit the criterion names, descriptions, and weights from the organizer desk. The API rejects any update whose weights do not add up to 100%, so a published rubric remains mathematically complete.

The organizer results response also includes a complete normalization proof:

- `normalization.judge_profiles` shows each judge's score count, mean, standard deviation, and every raw-to-normalized criterion conversion.
- `normalization.ranking_changes` shows raw rank, normalized rank, raw weighted score, normalized score, and rank movement.
- Each conversion includes raw and normalized weighted contributions, so the final aggregate can be independently recomputed.

The organizer dashboard presents this as `RAW → NORMALIZED → RANKED`, with the formula, judge calibration table, ranking movement table, and an expandable contribution ledger.

### Worked normalization example

The live endpoint uses the same method as this simplified example. Suppose Judge A is generous and scores `5, 5, 4` across Alpha, Gamma, and Delta, while Judge B is stricter and scores `4, 3, 2` across Beta, Gamma, and Delta. For each judge:

```text
mean = average(all scores for that judge)
stddev = population standard deviation(all scores for that judge)
normalized = clamp(50 + 10 × ((raw - mean) / stddev), 0, 100)
```

Judge A's `5` becomes approximately `57.07`, while Judge B's `4` becomes approximately `62.25`. Raw weighted scores rank Alpha (`5`) above Beta (`4`), but normalized scores rank Beta (`62.25`) above Alpha (`57.07`). The response and dashboard show the exact values used for the actual event, including whether a project's final rank moved.

## Weird or inconsistent judging

- A score must be an integer from 1 through 5.
- A judge can only update their own scorecard.
- Organizers cannot assign a judge to a project from that judge's own team.
- Missing criteria remain visible as incomplete in the judge queue.
- Private judge notes are not shown in the public gallery.
- An organizer can inspect the raw scoring record and the audit trail before publishing.

## Threat model notes

Community voting uses the signed-in user's identity as the voter key, rejects duplicate votes for the same project, and applies a rolling request limit. Unauthenticated API callers need an explicit local voter key and are still rate limited, so the local demo can support a public-link mode without making it the default. This does not claim to stop determined Sybil voters, VPN rotation, or malicious code in submitted demos. Demos and repository links are treated as untrusted external content.

The full security reference is in [`threat_model.md`](threat_model.md). It
covers the browser/API, role, database, public/authenticated, and webhook
boundaries, plus the required guarantees and known limits.

## Pairwise judging

Pairwise judging is an optional second signal for assigned judges. The next
comparison is selected deterministically from two assigned projects belonging
to different teams. The API rejects unassigned projects, unauthorized tracks,
same-team comparisons, duplicate comparisons, and winners outside the pair.

The organizer endpoint fits the recorded comparisons with a deterministic
Bradley–Terry maximum-likelihood model using a bounded minorization-maximization
update. It returns a normalized strength score and rank for every project.
Pairwise results remain separate from the official weighted scorecard.

```text
GET  /api/judge/pairwise
POST /api/judge/pairwise
GET  /api/organizer/pairwise
```

## Operational integrity features

- Webhook payloads are HMAC-signed and stored before delivery; a failed delivery is visible in the organizer delivery history and never breaks core judging.
- Certificates are issued by organizers, use unique verification codes, and render without an external PDF or cloud service.
- Bulk imports return both an imported count and row-level rejections. Full event JSON export is intended for backup and migration.

## Bonus evidence

The complete bonus-to-implementation map and verification commands are in
[`BONUS-EVIDENCE.md`](BONUS-EVIDENCE.md). The documented optional bonuses are
normalization proof (+5), pairwise judging (+5), threat model (+3), and
API-first design (+3).