# Local exam scheduling

Migration 0006 adds three nullable UTC timestamps. Unscheduled exams retain
existing behavior. The business/display timezone is Asia/Tehran.

Only the owning counselor of a non-personal exam may GET/PATCH
`/api/v1/exams/{id}/schedule/`. PATCH accepts any subset of:

```json
{
  "scheduled_start_at": "2030-05-01T12:30:00+03:30",
  "participation_deadline_at": "2030-05-01T13:30:00+03:30",
  "result_release_at": "2030-05-01T14:00:00+03:30"
}
```

Null clears an option. New timestamps must be future ISO 8601 values with an
explicit offset or Z. Deadline must follow start. The deadline is the exclusive
last entry time, not a forced provider-side finish: local entry is allowed at
start, and blocked at deadline. Release must be at or after the latest allowed
entry plus configured duration (deadline + duration, or start + duration when
no deadline exists). Null release preserves immediate local result access.

Any existing attempt locks all schedule changes, including clearing fields.
Identical no-op PATCHes remain valid. Schedule edits and student start claims/result persistence
use the same exam-row lock. Start commits a per-attempt UUID lease before HTTP;
schedule edits immediately reject that pending attempt. No lock is held during
provider HTTP. Before returning a link, start rechecks access, current identity,
archive/trash/status, deadline and provider code in a second short transaction. Responses include the current exam and `availability`
(code, Persian label, can_start, results_released, server_time). Counselor data
also includes schedule_locked. Persisted Exam.status is never auto-mutated.

Student result GET and POST return 403 / RESULTS_NOT_RELEASED before release,
without scores, rank, answers, or provider calls. Student list/detail do not
expose answer/key URLs and hide result_available during embargo. Counselor
result visibility retains existing ownership and student-responsibility rules.
Blocked starts return 409 / EXAM_NOT_STARTED or PARTICIPATION_DEADLINE_PASSED
before identity or provider operations. Assignment authorization still applies.

Duration remains configured at manual/automatic creation. The supported local
Quiz24 adapter has no duration or scheduling update method; remote support is
unverified. No provider operations were added or called live. This policy does
not revoke or restrict already-issued/direct Quiz24 hosted URLs or results.

The counselor panel uses native Gregorian date/time inputs, with Persian
calendar summaries and Tehran conversion. Student availability refreshes every
30 seconds and on focus/visibility; the backend remains authoritative.

Run the scoped exam suites using the existing isolated test settings. The row-lock regressions were verified on an isolated PostgreSQL 16
cluster in Sprint 7 and still skip on SQLite. Legacy and incremental migration
preservation are covered; see [QA_SPRINT7.md](QA_SPRINT7.md).

Apply after deployment review from the backend directory:

```sh
python manage.py migrate exams
```
