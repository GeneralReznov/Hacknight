# Data model

SQLite contains:

- `users` — local identities and roles.
- `events`, `tracks`, `prizes` — event configuration.
- `teams`, `team_members` — team formation and membership.
- `projects` — draft/submitted work tied to one event, team, and track.
- `criteria` — ordered, weighted judging rubric.
- `assignments` — the explicit judge-to-project access boundary.
- `scores` — one score per criterion within an assignment.
- `votes`, `comments` — public community feedback, tied to authenticated users when available.
- `rate_limits` — short rolling request buckets for votes and comments.
- `webhooks`, `webhook_deliveries` — signed event subscriptions and their delivery history.
- `certificates` — locally verifiable achievement records for submitted projects.
- `audit_log` — login, mutation, score, export, vote, comment, and denied-access records.

Every project belongs to an event and a team. Every score belongs to an assignment, which means a judge cannot create a score without being assigned to that project. A judge who is a member of the submitted project's team cannot be assigned to it. Export reads relational records directly into flat CSV or a complete JSON event package. Import creates missing teams/tracks while rejecting malformed rows instead of silently dropping them.