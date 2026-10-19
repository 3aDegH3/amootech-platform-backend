# Sprint 7 — integration QA and hardening

Date: 2026-10-19 (Asia/Tehran). Scope: exams, Quiz24 adapter, relevant
account/identity regressions, existing counselor/student exam UI.

**Production verdict: NOT READY for production sign-off.** Automated checks pass;
actual browser acceptance and a controlled provider sandbox smoke test remain
unverified. Hosted-link restrictions below must be accepted or addressed before
launch. This is not a claim of live Quiz24 compatibility or production DB state.

## Risk inventory and confirmed fixes

| Confirmed issue | Change and evidence |
| --- | --- |
| Student start held the exam transaction during provider HTTP | Short row-locked claim, HTTP outside transaction, second authorization/availability check; PostgreSQL schedule/start race verifies nonblocking rejection and `in_atomic_block=False`. |
| Simultaneous starts requested multiple URLs | Per-attempt UUID claim and 60-second lease; concurrent request gets START_IN_PROGRESS; one provider call/attempt. Expired claim can recover; stale response cannot release a newer claim. |
| Archive/deadline/identity/access could change while requesting a link | Recheck them before returning any URL; races return 404/409 without a URL. Claims never prove actual provider participation. |
| Direct starts could re-enter completed attempts despite unsupported retakes | Reject completed attempts before and after HTTP. Preserve scores/results. |
| Partial personal result refresh removed verification; empty answer arrays erased stored answers | Reject unverified replacement of a verified result; preserve prior answers when no valid new rows exist. |
| Individual result parser could mistake a course score or one of multiple summaries for overall score | Exclude Course-string rows; reject ambiguous summaries; do not guess timezone for naive provider timestamps. |
| Numeric exam codes were sent as strings on result/answersheet/participation calls | Convert numeric model codes to JSON integers in the adapter; wire-contract regression asserts school ID, mobile and code types. |
| Two counselor and two practice-builder effect lint errors | Reset dependent values in existing input/retry handlers and error state in asynchronous callbacks; keep existing stale-response guards. |
| Archived/trashed published exams appeared in the student's active section | Move them to past; retain authorized results and block start. Extend availability type with ARCHIVED/TRASHED. |

## Automated evidence

- Full isolated PostgreSQL suite: **178 passed, 0 skipped, 0 failed** (exams +
  accounts auth, profile, permissions, mobile/OTP/provider-link regressions).
- SQLite full regression before the final completion-race addition: **177 tests,
  168 passed, 9 PostgreSQL-only skipped**. Final hardening suite rerun separately:
  **14 tests, 10 passed, 4 PostgreSQL-only skipped**.
- Frontend exam/manual-auth tests: **69 passed**.
- `tsc --noEmit -p tsconfig.exams.json`: PASS.
- Scoped ESLint across counselor exams, student exams and shared exam components:
  PASS, including the previously failing builders.
- Migration drift (`makemigrations --check --dry-run`): PASS, no pending changes.
- Backend/frontend `git diff --check`: PASS.

PostgreSQL 16 was initialized in a private temporary Unix-socket-only cluster;
no application credentials or application database were used. Tests created and
destroyed `test_amootech_sprint7`. All provider HTTP/SMS boundaries were mocked;
OTP regressions use the recording development adapter, never real delivery.
No production migrations, live provider mutations, commit or push occurred.

The nine PostgreSQL concurrency tests cover: schedule during start; monitoring
single lease; analytics single lease; management version conflicts; editor
single claim; simultaneous starts with assignment removal; archive during HTTP;
simultaneous assignment additions; remote-delete timeout with concurrent mutation
and retry blocking. Existing monitoring/analytics tests verify provider HTTP is
outside row-locked transactions and preserve verified results after failure.

Integration evidence reuses existing manual/automatic creation, callback,
scheduling, monitoring, analytics, management and practice tests. The added
complete manual group lifecycle creates one provider exam once, assigns three
students, schedules/publishes, blocks early participation/results, starts an
eligible verified student, monitors completions, synchronizes group results,
checks 1,1,3 ranking and missing-score behavior through existing analytics tests,
archives/trashes/restores, and retrieves the student's preserved released result.
The endpoint matrix tests foreign-counselor, student and anonymous access;
student result selection cannot expose another student's record.

## Migration and data integrity

The graph is linear: exams 0004 → 0005 assignments → 0006 scheduling → 0007
monitoring → 0008 analytics → 0009 management → **0010 start claim fields**.
0005 additionally depends on accounts 0004. No conflicting leaves were found.

0010 adds nullable `participation_token` and `participation_lease_until` to
ExamAttempt; neither is exposed by student/counselor result serializers.
No data deletion/backfill is needed. Existing attempts and exam/student unique
constraints remain intact. The legacy 0004 → latest backfill regression was
updated to restore the latest schema. An incremental 0007 → 0008 → 0009 → 0010
regression preserves assignment, attempt, answer, monitoring, participation,
analytics cursor, provider identity, management history and operation records.
0009 correctly backfills manual creation and publication history without
pretending legacy results have verified identities.

A pending start intent remains a PENDING local attempt even after a provider
failure; it is not proof of a student starting or completing the hosted exam.
Its started_at is set only after an authorized link response. Such attempts
conservatively lock schedule/assignment removal/content editing; they are never
deleted to simulate a reset. An interrupted worker's start lease expires after
60 seconds. Management/delete UNKNOWN claims never expire or retry blindly.

Application/production migration application state was not inspected or changed.
Apply migrations only through the normal reviewed deployment process after
backup/restore checks. PostgreSQL migration compatibility was tested in isolation.

## Provider contract review (documentation only)

Source checked read-only: https://www.quiz24.ir/WebService/v1

| Operation | Contract reviewed / adapter behavior |
| --- | --- |
| createOnlineExam | School userId, integer grade/course/counts, comma-separated taxonomy IDs, duration, callback; parse returned exam code, never substitute exam ID. |
| createExam | New manual exam only; durable local creation receipt prevents duplicate/uncertain creation retries. Actual response variants remain mocked, not live-confirmed. |
| editExam | Current docs list this existing-exam method, not createExam with examId. Exact associated ID, callback/session fence, ownership and participation preflight; never replace exam. |
| exam | School userId plus examCode; parse exact ID/code, count and documented PDF fields. |
| examParticipation | Verified zero-prefixed mobile userName; numeric examCode, callback; isEditAnswersheet=false. Returns a hosted URL, not confirmed participation. |
| examResult | Verified mobile + code; overall finite Score/Balance, provider Rank/RankInClass kept distinct. Score scale UNKNOWN; group rank provisional; absent score excluded from aggregates. |
| answersheets | Same mobile/code; accept only unambiguous exam-wide answer order, preserve previous answers on missing/empty/failed responses. |
| getExamParticipants | Invited participants only, paged, explicit booleans; unknown/missing identities never become nonparticipation proof. |
| deleteExam | Disabled by default; individual exact-ID/title confirmation; no published/assigned/attempted exam; UNKNOWN response blocks retry and needs reconciliation. No destructive smoke test. |

Adapter HTTP timeout is 15 seconds, with no automatic HTTP retries. Monitoring
has a 60-second cooldown, 90-second fenced lease, 20-page/30-second budget.
Analytics has a 60-second cooldown, 90-second lease, 10-student/30-second budget.
Creation and destructive operations have durable receipts/claims. Starts have a
60-second in-flight lease; sequential continuation requests may ask for another
URL to the same provider exam. There is no global provider-quota limiter for
individual result/taxonomy/start requests: deployment rate limits and provider
quotas still need operational configuration. No undocumented reset, forced
finish, link revocation or provider-side schedule API was introduced.

## Explicit deployment risks and remaining acceptance work

1. Previously issued/direct Quiz24 participation URLs cannot be revoked locally.
   Archive, trash, unpublish, reassignment and later schedule changes cannot
   guarantee blocking those hosted links. A link already in flight may be
   generated externally but is withheld locally if final authorization fails.
2. Local start/deadline is an entry policy, not a forced provider finish time.
   Local timestamps are UTC; the UI formats and converts Asia/Tehran; start is
   inclusive and deadline exclusive. Provider settings must be aligned separately.
3. Local result embargo protects local APIs/UI only. Hosted Quiz24 results,
   answers, PDFs or already downloaded material may bypass local release time.
4. External editor URLs/tabs cannot be invalidated through documented APIs.
   Save and close the editor before reconciliation; local version fences cannot
   prevent a user revisiting an older external editor tab.
5. The participant listing covers invited users; an empty list does not prove
   globally absent participation. Automatic content editing, answersheet edits
   and retakes remain unsupported because provider eligibility cannot be proved.
6. Unknown creation/deletion or abandoned management claims need operator
   reconciliation. No unsafe force-unlock or blind destructive retry is exposed.
7. Browser/mobile/keyboard rendering and actual provider response compatibility
   are not verified by static markup/unit tests. Provider quotas, hosted settings
   and production DB application state require deployment review.

## Manual browser acceptance checklist

All statuses below refer to **actual browser/provider testing**, not mocks.
No row has been live-verified in this session.

| # | Check | Status |
| --- | --- | --- |
| 1 | Counselor login; expired-session recovery returns to owned exam workspace | NOT TESTED |
| 2 | Create one manual exam using an approved sandbox account | NOT TESTED |
| 3 | Save questions, close editor, return; reconcile same ID/code without duplication | NOT TESTED |
| 4 | Assign multiple owned students; reject foreign student selections | NOT TESTED |
| 5 | Set Tehran schedule/release, publish; verify responsive RTL and keyboard dialogs | NOT TESTED |
| 6 | Verified student login and hosted participation in approved sandbox only | NOT TESTED |
| 7 | Block early/late local entry; separately inspect/document hosted-link bypass | NOT TESTED |
| 8 | Monitor explicit participation; distinguish URL request from actual start | NOT TESTED |
| 9 | Synchronize results; retry a failed read without losing stored results | NOT TESTED |
| 10 | Validate ties, missing Score, separate provider Rank/Balance and unknown units | NOT TESTED |
| 11 | Archive, restore, trash, bulk actions; no provider deletion; test mobile/focus | NOT TESTED |
| 12 | Old results remain available only to authorized users after local release | NOT TESTED |

Optional controlled smoke procedure: first obtain approval for a dedicated
provider sandbox school and synthetic verified students; keep remote deletion
false and real SMS disconnected. Record provider exam ID/code before editing,
use one manual and one automatic disposable exam, and run the checklist with
provider-side dates/results settings explicitly recorded. Capture redacted
contract samples/status codes only, never keys, real student answers or phone
numbers. Stop on identity mismatch, ambiguous creation or unexpected participation.
Do not run remote deletion as part of this checklist.

## Reproduce isolated automated checks

From the backend directory (PostgreSQL binaries installed locally):

```sh
qa_pg_dir=$(mktemp -d /tmp/amootech-sprint7-pg.XXXXXX)
/usr/lib/postgresql/16/bin/initdb -D "$qa_pg_dir/data" --auth-local=trust --auth-host=reject --no-locale -E UTF8
/usr/lib/postgresql/16/bin/pg_ctl -D "$qa_pg_dir/data" -l "$qa_pg_dir/server.log" -o "-k $qa_pg_dir -c listen_addresses= -p 55437" -w start
EXAM_TEST_PG_SOCKET="$qa_pg_dir" .venv/bin/python manage.py test apps.exams.tests apps.accounts.tests --settings=apps.exams.tests.postgres_settings --noinput
/usr/lib/postgresql/16/bin/pg_ctl -D "$qa_pg_dir/data" -m fast -w stop
.venv/bin/python manage.py makemigrations --check --dry-run --settings=apps.exams.tests.settings
```

From the frontend directory:

```sh
node --experimental-strip-types --test tests/*exam*.test.mjs tests/manual-auth.test.mjs
npx tsc --noEmit -p tsconfig.exams.json
npx eslint src/app/counselor/exams src/app/student/exams src/components/exams
```

## Files changed in Sprint 7

Backend: models.py, views.py, services/quiz24.py, services/results.py;
migration 0010; tests/postgres_settings.py, test_hardening.py,
test_migration_integrity.py, test_assignments.py, test_scheduling.py,
test_integration.py; SCHEDULING.md, ASSIGNMENTS.md, ANALYTICS.md,
MONITORING.md, MANAGEMENT.md and this report (paths relative to apps/exams).
Frontend: src/app/counselor/exams/page.tsx,
src/app/student/exams/PracticeBuilder.tsx, studentExam.ts,
src/components/exams/schedule.ts, tests/student-exams.test.mjs.
Existing unrelated working-tree changes were retained.
