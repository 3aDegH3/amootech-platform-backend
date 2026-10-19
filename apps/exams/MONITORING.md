# Counselor participant monitoring (Sprint 4)

Public contract inspected: https://www.quiz24.ir/WebService/v1#getExamParticipantsBox
The documented endpoint lists **invited** participants, accepts examCode and
page, and exposes userName, fullName, isStarted, isComplete, persianDoDate.
There is no documented stable external student ID or precise timezone-aware
completion timestamp in this listing. It is not an invitation mutation API.
Only public documentation was read; provider API requests are mocked in tests.

A successful examParticipation call only issues a URL. Existing PENDING local
attempts/started_at therefore do not prove provider participation. A local
COMPLETED attempt from the existing result flow is retained as completion
evidence. Monitoring never edits attempts, answers, scores, schedules or status.

## API

Only the owning counselor of a non-personal exam may access:

- GET /api/v1/exams/{id}/monitoring/?q=name&status=UNKNOWN&page=1
- POST /api/v1/exams/{id}/monitoring/sync/

GET returns DRF count/next/previous/results plus unfiltered summary totals,
last_success_at, last_attempt_at, fresh, fresh_until, partial, error, syncing, retry_after,
and existing scheduling availability. All totals and rows include only current
responsibility assignments. Rows contain local student ID/name, status,
last_known_status, observed_at, verified timestamps when available, local
attempt status/request timestamp, absence flag and identity warnings. No scores,
rankings, phone numbers, provider-wide counts, or unmatched personal data.
POST returns the updated summary/freshness; failures retain prior evidence.
The UI reloads the current server-filtered page after sync.

## Evidence and identity

- NOT_STARTED requires explicit boolean isStarted=false/isComplete=false.
- IN_PROGRESS requires explicit true/false.
- COMPLETED requires true/true or an existing local COMPLETED result attempt.
- Missing, conflicting, malformed, failed, superseded or stale nonterminal
  evidence is UNKNOWN; previous evidence stays available as last_known_status.
- An empty invited list never establishes that local assignees did not start.
- Absence requires fresh explicit non-start evidence observed at/after deadline.
- Terminal completion is monotonic; uncertain/empty responses cannot erase it.

The documented userName is reconciled to the existing verified mobile login
used by registration/examParticipation. Numeric username normalization restores
the leading zero only for ten-digit Iranian mobile logins. Both mobile
verification and the SYNCED external ID/current Quiz24 school mapping are
required. No display-name matching or invented provider ID fields are used.
Nonterminal observations are bound to that external ID, school and verification
time. Unknown provider people are discarded; only a coverage warning remains.

persianDoDate has no sufficiently specified parsing/timezone contract, so it is
not guessed or stored. Provider start/end timestamps remain unverified. An existing completed
attempt's finished_at is exposed only under local_attempt, with local provenance;
it is not claimed to be a documented provider completion timestamp. URL issuance times
remain explicitly local and are not displayed as verified provider start times.

## Sync and persistence

Migration 0007 creates ExamMonitoring (timestamps, safe error codes, lease) and
ExamParticipation (minimal assignment-bound verified evidence/identity binding).
No raw participant bodies/names/mobile numbers are stored. Existing exam rows
and results are preserved. GET never calls the provider or writes state.

POST claims a fenced 90-second lease in a short transaction; HTTP runs outside
transactions, then a second short transaction rechecks exam and assignment
ownership/identity before bulk persistence. Cooldown is 60 seconds, including
failures. Each sync traverses at most 20 documented pages, stops at an empty
page and rejects repeated pages. Network time budget is 30 seconds, with each
request timeout at most 15 seconds. No per-student provider calls, worker, or
polling. Provider exam ID is not required: the documented API uses examCode.

Nonterminal evidence expires after five minutes and must belong to the latest
successful traversal. A partial traversal can confirm individually valid rows,
but missing/malformed/unmatched entries remain unverified. Page/transport
failure commits no partial observations and preserves last successful sync.

Run scoped exams tests with apps.exams.tests.settings (isolated SQLite).
PostgreSQL row-lock tests skip on SQLite and passed on the isolated PostgreSQL 16
cluster in Sprint 7; see [QA_SPRINT7.md](QA_SPRINT7.md). No live API behavior or
hosted-link restriction is claimed. A PENDING start intent may have no issued URL;
started_at is populated only when the final authorized URL response is returned.

Apply after deployment review from the backend directory:

```sh
python manage.py migrate exams
```
