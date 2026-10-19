# Sprint 6: counselor exam management

Scope: counselor-owned, non-practice exams. Existing DRAFT → READY → PUBLISHED
and CLOSED statuses remain unchanged. Archive/trash are independent timestamps.
All management mutations lock the exam row; local requests carry
`expected_updated_at`. Stale selections fail; bulk operations (1–100 distinct
exams, sorted locking) roll back entirely on any failure. No physical exam,
assignment, attempt, answer, result or analytics deletion occurs in these actions.

## API (under /api/v1/exams/)

- `GET/POST {id}/management/`: capabilities; POST action + expected_updated_at,
  and title for rename. Actions: rename, unpublish, archive, unarchive, trash, restore.
- `POST {id}/publish/`: existing endpoint; optional expected_updated_at supported.
- `POST management/bulk/`: action archive/unarchive/trash, exams array containing
  id and expected_updated_at. Atomic; rejects foreign ownership and duplicates.
- `GET {id}/history/`: paginated, read-only audit events, scoped to owner.
- `POST {id}/edit-questions/`: UUID request_key + expected_updated_at. Returns
  edit_url + edit_session. An existing pending/editing/unknown claim blocks mutations.
- `POST {id}/sync/`: existing manual return flow, with edit_session while editing.
  Token, owner, version, exact provider ID/code and activity rechecked before write.
- `POST {id}/remote-delete/`: UUID request_key, expected_updated_at,
  confirm_remote_delete=true, exact provider_exam_id and confirmed_title.

## Local transitions

Rename: any non-trashed exam without an unresolved provider operation.
Publish: READY, positive question count, provider code, no archive/trash/claim.
Unpublish: PUBLISHED → READY only without any known participation or attempt.
Archive/trash: any lifecycle status without a provider claim; never contact Quiz24.
Unarchive/restore: published records become READY without activity, CLOSED with
activity, avoiding accidental reopening. Restore retains any archive timestamp.
Trash prevents rename/archive/unarchive until restored. Matching target operations
with a current version do not create duplicate events. Historical result release
and student-specific result permissions continue to apply.

Archived/trashed exams cannot receive new participation URLs from Amootech.
Previously issued Quiz24 links cannot be revoked by these local controls.

## Provider capabilities and conservative limits

Documentation checked read-only on 2026-10-19:
https://www.quiz24.ir/WebService/v1

The current official documentation lists **editExam**, not createExam, for
editing existing exams (userId, examId, optional title, callback). Consequently
`edit_existing_exam` uses editExam and never falls back to createExam.
Only manual, never-published DRAFT/READY exams without participation are eligible.
Previously published exams are conservatively excluded because old student links
cannot be revoked. Automatic and UNKNOWN creation modes are excluded.
A newly created manual draft lacking provider ID resumes its original persisted
manage URL; it does not create another exam. Callback/session context is retained
through login; identity is reconciled through the existing exam detail API.

Provider read-only identity and complete participant preflight precedes mutations.
A durable one-per-exam operation claim fences local changes without holding a DB
transaction over provider HTTP. Exact confirmed status 100 is needed for success.
Timeouts or malformed mutation responses become UNKNOWN and block blind retries;
failed read-only preflight is FAILED. Pending claims do not automatically expire.
A stale response/session cannot replace newer local metadata.

QUIZ24_REMOTE_DELETE_ENABLED defaults false. Remote deletion additionally requires
trash, never published, no assignments, no attempt/participation, exact ID/title
and separate confirmation. It never runs through archive, trash or bulk endpoints.
Local history survives even after confirmed remote deletion. UNKNOWN deletion or
abandoned PENDING operations require operator investigation; no automatic retry or
unsafe force-unlock endpoint is provided.

No reset API exists here. isEditAnswersheet requires provider-enabled answersheet
editing; multiple participation is documented only for floating type 2. The local
model cannot prove these settings, so reset/retake/answersheet editing is explicitly
unsupported. No replacement exam, erased attempt or artificial ranking reset.

Provider editor tabs/URLs cannot be revoked by the documented APIs. Counselors
must save and close the external editor before reconciliation; local fencing
cannot guarantee invalidation of a previously issued external tab or link.

Migration 0009 adds archive/trash/creation mode, publication history, audit events
and provider-operation claims. Existing manual manage URLs are conservatively
backfilled as MANUAL; PUBLISHED/CLOSED as ever_published. No production migrations
are applied by development checks. Audit APIs expose no update/delete route and
contain only actor, local metadata, action, timestamp and outcome.

## Validation

`manage.py test apps.exams.tests --settings=apps.exams.tests.settings --noinput`
uses isolated SQLite and mocks provider calls. PostgreSQL-only concurrency tests
are explicitly skipped there; run with an isolated PostgreSQL test configuration
to verify actual row locking. No live provider mutations are required or permitted.

## Files completed in this pass

Backend: `.env.example`, `apps/exams/views.py`, `apps/exams/serializers.py`,
`apps/exams/services/management.py`, `apps/exams/services/quiz24.py`,
`apps/exams/tests/test_management.py`, `apps/exams/tests/test_integration.py`,
and this document. Existing Sprint 6 model fields, settings and migration 0009
were reused from the working tree.

Frontend: `src/app/counselor/exams/ManagementPanel.tsx`, `page.tsx`,
`workspace.ts`, `workspace.css`, `src/components/exams/manualFlow.ts`,
and `tests/exam-management.test.mjs`.

Final checks: 121 Django tests (116 passed, 5 PostgreSQL-only skipped),
68 frontend tests passed, scoped TypeScript passed, new-code ESLint passed.
Full counselor page ESLint still reports two pre-existing
react-hooks/set-state-in-effect errors in the automatic builder at lines 103/115.
Local PostgreSQL connection with the development account failed, so actual
PostgreSQL concurrency remains unverified. Migration drift check and diff
whitespace checks passed. No production migration, commit or push performed.

## Sprint 7 revalidation

The historical Sprint 6 test status above is superseded by
[QA_SPRINT7.md](QA_SPRINT7.md). All nine PostgreSQL concurrency regressions
passed in an isolated temporary PostgreSQL 16 cluster. Full scoped frontend
ESLint now passes. Migration 0010 adds nullable start-claim fields; failed start
intents remain historical PENDING records and do not prove participation.
No production migration or live provider mutation was performed.
