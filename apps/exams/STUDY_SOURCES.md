# Study report sources — Sprint 5

## Source audit

`DailyReport.student` owns a date; `DailyReportItem` is an individual occurrence.
Only its explicit `reported_status` of `COMPLETED` (the existing stored value for
DONE) or `PARTIAL` makes an academic activity eligible. The API preserves these
stored values. `NOT_DONE`, NULL/UNSET, execution completion, timers, study minutes,
test counts, and a published plan by themselves do not establish eligibility.
Opening a report pre-populates plan rows with NULL declarations; those are excluded.
Legacy rows are not backfilled or interpreted as declarations.

Planned rows inherit the current `PlanItem` title and academic context. Their plan
must remain published and belong to the report student and date. `PlanItemExecution`
is independent and is never consulted by this engine. Outside-plan rows inherit
their own kind/title/internal academic FKs. They are eligible under the same explicit
status rule, including imported free-text-only records. EVENT rows are excluded;
STUDY/TEST/REVIEW/EXAM are the existing academic kinds.

The plan FK is protected from deletion and report deletion cascades to report items.
No extraction records or historical snapshots are persisted. Subsequent report,
plan, academic metadata, or student-profile edits affect subsequent reads. No
existing exam is read or changed. Repeated topics remain separate occurrences.

## Read-only contract

`GET /api/v1/exams/study-sources/`

- Default: `period=today`, in Asia/Tehran, independent of the server timezone.
- `period=week`: Saturday through today, following planning's Saturday-first week.
- `start_date=2026-10-01&end_date=2026-10-21`: inclusive custom ISO/Gregorian dates.
  `period=custom` is optional with both dates. Do not combine dates with another period.
- Custom ranges may be historical, but must end by today and span at most 90 days.
- `page` and `page_size` use page-number pagination (default 20, maximum 100).
- Counselors may supply `student_id`, or omit it to read currently assigned students
  within the requested range. Admins must supply `student_id`, following the existing
  student-specific report convention. Students must omit both `student`
  and `student_id`; even an override naming themselves is rejected with 403.

The response contains `schema_version: 1`, `student_id` (null for a counselor's
combined assigned-student scope), `range` (period, start/end
dates, timezone), standard `count`/`next`/`previous`/`results`, `summary` and
`page_summary`. Ordering is descending report date/report ID, then activity ordering
and report-item ID. Counts are not affected by pagination:

```json
{
  "summary": {
    "scope": "filtered_range",
    "by_status": {"COMPLETED": 3, "PARTIAL": 1},
    "by_kind": {"STUDY": 3, "TEST": 0, "REVIEW": 1, "EXAM": 0}
  },
  "page_summary": {
    "scope": "current_page",
    "by_classification": {"STRUCTURED": 3, "FREE_TEXT": 1},
    "by_state": {"SUGGESTED": 2, "NEEDS_CONFIRMATION": 1, "NEEDS_SELECTION": 1},
    "unresolved_academic_context": 1
  }
}
```

Context counters describe only the current page; absent classifications/states
have zero occurrences. This keeps validation and hydration bounded to a page.
Range-wide status/kind totals are computed in the database. Academic permission
queries are batched across students in a page without merging their individual scopes.
A valid empty result
returns count zero, empty results and zero status/kind counters.

Every result supplies:

- `source_ref` (`daily-report-item:<id>`), `report_item_id`, `report_id`,
  `report_date`, `student_id`, stored `reported_status`, and mutation `source`.
- `source_revision`: an opaque stale-context digest added for Sprint 6; it does not
  replace ownership/eligibility checks. See [report topic selection](REPORT_TOPICS.md).
- `kind`, `title`, `title_is_custom`, nullable `plan_item_id` and `plan_id`.
- `classification`: `STRUCTURED` or `FREE_TEXT` (presence of saved context, not validity).
- `taxonomy_snapshot`: saved `provider_context` and selected internal subject/chapter/topic IDs.
- `suggested_selection`: nullable `{namespace: "academics", hierarchy, deepest_level}`.
  Hierarchy nodes contain real internal IDs and current labels, never provider mappings.
- `state`, `academic_context_usable`, `needs_taxonomy_selection`,
  `requires_topic_confirmation`, and machine-readable `reasons`.

## Context semantics and access

A valid internal course/chapter/topic hierarchy produces `SUGGESTED` and its deepest
valid node, deriving missing ancestors only through actual database relationships.
Conflicting FKs, inactive nodes, invalid profile grade/field, and subjects outside
the student's current educational period/field cannot produce usable suggestions.
The canonical Sprint 1 `student_period`/`scope_catalog` policy is evaluated as the
target student, also when a counselor/admin makes the request.

A free-text activity produces `NEEDS_SELECTION`. No text is parsed into IDs.
A custom title alongside structured context produces `NEEDS_CONFIRMATION` with
`CUSTOM_TITLE_MAY_DESCRIBE_ADDITIONAL_TOPICS`, retaining any valid internal suggestion.
The original nonblank title is preserved verbatim. A blank legacy internal title
uses the existing generated-title rule for display.

Quiz24 context is returned as saved provenance, **not** as verified selection or
catalog access. Well-shaped saved metadata is `NEEDS_CONFIRMATION` with
`PROVIDER_CONTEXT_REQUIRES_REVALIDATION`; malformed metadata uses
`INVALID_SAVED_TAXONOMY`. There are no provider or cached catalog calls. Current
provider ancestry/period/field cannot be revalidated offline, especially when
intermediate ancestors were not saved. Sprint 6 must resolve that context through
the authorized catalog; no missing ancestor or provider ID is fabricated here.
Mixed internal/provider metadata is unresolved (`MIXED_TAXONOMY_NAMESPACES`).

Even `SUGGESTED` is only a starting scope. `requires_topic_confirmation` is always
true: neither COMPLETED nor PARTIAL establishes which exact topics were studied.
An unusable context never falls back to an unrestricted catalog or full course.

Students read only their authenticated profile's reports. Counselors select only
currently assigned students (404 for foreign/unassigned IDs); admin follows the
existing global-read convention. No supervisor role exists in the current role model.
Unknown/missing student profiles return 404. Dates/query errors return 400; a page
outside the available result range returns 404. This endpoint supports GET only.

## Validation

```sh
.venv/bin/python manage.py test apps.exams.tests.test_study_sources apps.exams.tests.test_question_bank apps.daily_reports.tests.test_quick_reports --settings=apps.exams.tests.settings
```

Tests block provider network/catalog calls and cover explicit reporting, legacy and
unplanned rows, title ambiguity, internal and saved provider context, malformed and
out-of-period metadata, pagination/query budget, date boundaries, access and reassignment.
PostgreSQL uses the existing isolated `apps.exams.tests.postgres_settings` with a
temporary UTF-8 cluster; never use production settings for these tests.

Verified on 2026-10-21: 43 targeted tests passed on SQLite and the same 43 passed
on PostgreSQL, with no database-specific skips. This includes 21 extraction tests.
The full frontend TypeScript check, ActivityEditor ESLint, and 18 existing planner
regression tests also passed after removing the isolated stray identifier.

No schema changes, migrations, new dependencies, UI, question counts, or exam creation
are introduced. The separate ActivityEditor correction only removes its stray `k`.
