# push-events/

Written by `scripts/push_events.py` on every ~5-minute `fetch-highlights` run.
It's the source of push notifications for favourite teams: each detected event is
logged here and POSTed (`--mode send`) to the `pushEvents` Cloud Function. That
function notifies the subscribers of both teams, using the topics devices join from
the app's Menu → Notifications. The post is authenticated with the
`PUSH_EVENTS_SECRET` Actions secret, which holds the same value as the Firebase
secret.

- `events-YYYY-MM.jsonl`: one detected event per line. The types are:
  - `goal`: the total score went up;
  - `disallowed`: the total score went down (a VAR or data correction);
  - `final`: the final score has been unchanged for 15 minutes;
  - `highlights`: a clip was attached to the match.

  Each line carries the teams (football-data ids and names), the score, the
  competition, the kick-off time, `detected_at` and `minutes_after_kickoff`, so a
  real matchday can be measured.
- `ledger.json`: the keys already emitted (`{match_id}:goal:{h}-{a}`,
  `{match_id}:final`, …), plus the internal `{match_id}:ft:{h}-{a}` marks for when
  a final score was first seen. It means the same event is never reported twice.
  Entries older than 4 days are pruned.

Only matches that kicked off recently are considered: 6 h for goals and results,
72 h for highlights. A backfill of old data therefore produces no events.
- `pending.json`: events whose post failed, retried on the next runs. They're dropped
  once stale: goals and corrections after 30 min, results after 2 h, highlights after
  12 h.
