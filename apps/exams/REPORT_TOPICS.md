# Report-based exam builder — current workflow

Students choose today, the Saturday-first week, or custom dates. The builder loads
`GET /exams/study-sources/topics/` automatically: only eligible sources with verified
Quiz24 scope appear as deduplicated course/chapter/topic groups. Every checkbox starts
unchecked. Internal/free-text/invalid legacy context is excluded without guessing IDs.
Repeated occurrences retain their original report-item IDs and revisions per topic.

`POST /exams/study-sources/topics/preview/` reuses Sprint 6 inventory and taxonomy
validation, with strict report-scope checks before and after inventory. Source links
contain only topics within each occurrence's verified scope. Selecting a descendant
replaces a checked ancestor; incompatible mixed depths produce an inline error.

Difficulty counts and duration appear below checked topics. One create button submits
one existing `/exams/automatic/` request per explicitly selected course, including
`report_context`. There is no report-card selection or extra preview/confirmation step.
The server revalidates ownership, eligibility, revision, current permissions, exact
provider scope and inventory immediately before claiming provider creation.

Existing createOnlineExam, receipt deduplication/recovery, student identity checks,
owned exam/assignment persistence, history and start-link service are reused.
`ExamCreation.confirmed_scope` stores the confirmed taxonomy, immutable source snapshots
and report-item references. Migration: `python manage.py migrate` (includes exams 0011).
No production migration or live provider mutation was performed.

Additional checks: `apps.exams.tests.test_report_builder`, report-topic frontend unit
checks and `RUN_REPORT_TOPICS_BROWSER=1 node --test tests/report-topics-browser.test.mjs`.
The mobile browser mocks creation, including retry with the same key, inventory errors,
double-click prevention, separate course exams and participation links.

The following section documents the earlier Sprint 6 APIs, which remain available.

# Report-based topic selection — Sprint 6

This sprint adds **selection and advisory availability only**. It never calls
`createOnlineExam`, persists report coverage, changes planning/reports, or writes
taxonomy mappings. Sprint 4's independent question bank still creates personal exams
through its existing flow. Report-based creation remains Sprint 7 work.

## Student workflow

The exam center keeps two visible entry points: بانک سؤال and آزمون از گزارش کار.
The report flow offers today, the current Saturday-first week, or a custom range
(the existing Sprint 5 maximum of 90 days, ending no later than Tehran today).
Students select COMPLETED/PARTIAL activity cards across pages. The selected-source
summary explicitly retains activities when changing pages or date filters.

The next step resolves saved Quiz24 suggestions against the authorized catalog.
Internal academics IDs and free-text titles are never mapped or matched to provider
IDs. Free-text activities start with the profile's grade, mapped by its canonical
label only as a convenience; every permitted period grade remains selectable.
Original titles and saved scope are displayed separately, with custom-title warnings.

Suggestions are **not** checked as studied coverage. A student may explicitly apply
the deepest verified node, refine it, clear it, or choose another permitted course.
Selecting a whole chapter is labelled as including all its topics; it does not mark
its descendants completed. The existing TopicPicker has an optional exact-selection
mode; the Sprint 4 hierarchy parser and catalog/count services are reused.

Course drafts keep separate source associations and explicit topic choices. Students
choose which activities the current scope applies to. Switching courses and closing
the report panel preserve drafts in memory for the current page session. Refreshing
or leaving the page clears them; there is no new persistence model or browser storage.
Every preview requires explicit confirmation. Going back or changing the scope,
sources, question type, or used-question option invalidates that confirmation.

## API

Both new POST endpoints are student-only; counselors/admins retain Sprint 5 reads
but cannot use report-based preview/selection endpoints in this sprint.

Sprint 5 `GET /api/v1/exams/study-sources/` now adds an opaque `source_revision`
digest to each occurrence. Its existing fields and eligibility rules are unchanged.

`POST /api/v1/exams/study-sources/resolve/`

```json
{
  "sources": [{"report_item_id": 42, "source_revision": "<64-character digest from GET>"}]
}
```

Response: `schema_version: 1`, `default_grade_id`, and current source snapshots with
`resolution: {state, reason, suggestion}`. A verified suggestion includes authorized
`grade_id`, `course_id`, actual provider labels, and `topics` with `level`, `id`,
`label`, and complete actual `path`. A course-only suggestion has an empty topics
list. Sparse saved ancestry can be completed only from the actual provider tree.
Invalid, removed, mismatched, mixed, or unauthorized saved context returns
`NEEDS_SELECTION` with no suggestion. Provider transport/configuration errors remain
visible errors rather than fabricated empty trees. No global mapping is created.

`POST /api/v1/exams/study-sources/preview/`

```json
{
  "sources": [{"report_item_id": 42, "source_revision": "<digest>"}],
  "grade_id": 700,
  "course_id": 900,
  "topics": [{"level": "object", "id": 903}],
  "confirmed": true,
  "question_type": 1,
  "consider_used_question": false
}
```

`sources` is nonempty, unique, and limited to 100; `topics` is nonempty, unique, and
limited to 200. Only chapter/subject/object terminal nodes are accepted. Topics
contain only `level` and `id`: the server derives parent filters, never trusts
client-supplied ancestry, and rejects unknown top-level parameters/owner overrides.
The authenticated student owns every source; it must still be eligible and have
the same current revision. A foreign/missing/deleted/ineligible source yields one
generic validation error. Changed report status/title/context requires a new GET
and explicit selection. Relevant source/profile validation runs again after count
responses so in-flight changes cannot produce a successful stale preview.

The canonical Sprint 4 grade/course/field authorization and taxonomy index validate
every selected node. Internal IDs are never converted into provider IDs. Because
students may correct a plan, a confirmed course may differ from saved context;
the original snapshot remains intact and a visible warning identifies that correction.
This establishes a student declaration, not proof of studied coverage.

Response includes:

- `schema_version: 1`, `course_scoped: true`, `confirmed: true`,
  `scope_origin: "student_confirmed"`, `exam_created: false`.
- Actual `grade_label`/`course_label`, normalized `selected_topics` with real paths,
  and exact `provider_filters` (`grade_id`, `course_id`, complete ancestor arrays,
  `question_type`, `consider_used_question`).
- Normalized `availability`: `easy`, `medium`, `hard`, `very_hard`.
- Current `sources` snapshots and `source_links` connecting each stable source ref
  and revision to the confirmed topics. Sprint 7 can persist these after revalidation.
- `warnings` covering advisory inventory, zero availability, whole-chapter breadth,
  custom titles, or corrections to a different course.

## Exact scope and limits

Counts use the existing `Quiz24Service.question_count` / hardLevelQuestionCount
adapter, once per preview. Complete ancestor filters are built from verified paths.
Explicitly overlapping parents/descendants are normalized to the selected parent,
counted once and labelled with its breadth. Disjoint terminal nodes must use a common
depth. Mixing a whole chapter with an object in another chapter is rejected, since
the existing provider contract cannot establish that mixed-depth union precisely.
The UI explains this instead of silently expanding/narrowing the selection.

One preview has exactly one grade/course. Other course drafts are retained for later
previews, never combined or summed into one provider exam. Empty provider trees have
no selectable report topics; they do not enable an automatic whole-course fallback.
Zero inventory is a valid advisory response with a clear warning. Missing/malformed
counts are provider errors. Availability is not a reservation or guarantee of creation.

The existing provider catalog cache (15 minutes) is reused. Revalidation checks
against that server-authoritative cached catalog; provider removals may take until
cache refresh to appear. Sprint 7 must revalidate sources, permissions, scope and
inventory at actual creation time. Digests are stale-data checks, not authorization
tokens; all ownership and permission checks still run on every request.

## Tests

```sh
.venv/bin/python manage.py test apps.exams.tests.test_report_topics apps.exams.tests.test_study_sources apps.exams.tests.test_question_bank apps.daily_reports.tests.test_quick_reports --settings=apps.exams.tests.settings
node --experimental-strip-types --test tests/report-topics.test.mjs
RUN_REPORT_TOPICS_BROWSER=1 node --test tests/report-topics-browser.test.mjs
npm run test:question-bank
npm run test:question-bank:browser
npx tsc --noEmit --incremental false
npx eslint src/app/student/exams tests/report-topics.test.mjs tests/report-topics-browser.test.mjs tests/question-bank-browser.test.mjs
```

Tests mock provider reads and block unexpected network/creation calls. PostgreSQL
checks use a disposable UTF-8 cluster and the existing isolated test settings.
Validation on 2026-10-21: 58 targeted Django tests passed on both SQLite and
PostgreSQL, with no skips; 27 frontend unit tests, both report-topic and existing
question-bank mobile Chrome flows, full-project TypeScript and scoped ESLint passed.
The final additional container-grade/course ancestry checks also passed the 15
report-topic tests on SQLite and PostgreSQL.
No migrations, dependencies, provider mutations, production migrations or deployment
are required.
