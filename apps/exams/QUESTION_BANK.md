# Student question bank

The existing student PracticeBuilder and `/exams/automatic/` flow are reused.
The exam center exposes **بانک سؤال** and **آزمون از گزارش کار**. The latter now
supports Sprint 6 report/topic selection and advisory counts, with creation deferred
to Sprint 7; see [report topic selection](REPORT_TOPICS.md).
There is no dependency on study plans, reports, or recorded topic coverage.

## Server access and scope

The Sprint 1 policy derives periods from authenticated `StudentProfile.grade.number`:
1–6, 7–9 or 10–12. Internal grade IDs are never interpreted as Quiz24 IDs.
Provider grade labels match canonical names or the exact canonical name plus the
verified student field (e.g. دهم تجربی). Courses must reference that provider grade.
Upper-secondary courses require matching `fieldName` or an explicitly field-specific
provider grade; conflicting metadata is denied. Missing/ambiguous mapping fails closed.
A course ID may legitimately occur in several provider grades: lookup uses the
(gradeId, courseId) pair, never global course-ID uniqueness.
Duplicate provider grade identifiers are excluded from student authorization, even
when one of their labels belongs to an allowed period.
Counselors/admins retain their existing catalog and creation rules.

Existing endpoints remain:

- GET `/exams/provider/grades/` and `/exams/provider/courses/`
- POST `/exams/provider/course-detail/`
- POST `/exams/provider/question-count/`
- POST `/exams/automatic/`

The student course-detail response is checked for the requested course and consistent
parent links. Every submitted chapter/subject/object ID is checked against the cached
provider tree before inventory requests and new provider creations. Duplicate,
foreign, mismatched, malformed, or missing nodes fail validation; they are never dropped
to broaden the scope. Multiple IDs are accepted through the established array fields.
Explicit ancestor metadata is checked against the enclosing tree for both raw and
normalized responses. The frontend also rejects duplicate nodes, conflicting roots,
missing labels and malformed normalized trees instead of treating them as an empty course.
The authenticated profile is read again after availability returns, before claiming a
new createOnlineExam request. Student owner IDs in input remain forbidden.

No additional Quiz24 methods or client engine were added. Shared cached grades,
courses and courseDetail responses use the existing 15-minute cache. Course-only
creation can proceed when the catalog legitimately has no deeper nodes; unavailable
or malformed hierarchy cannot validate a selected topic.

## Creation and UI

Grade/course changes reset descendants. Grades and courses are clickable tiles. Optional chapter, subject and object
stages show compact checkable rows with an explicit انتخاب همین محدوده action.
Course/chapter breadth is shown before confirmation. Object selection includes its
required ancestors; compatible terminal selections use one common depth. A deeper selected level
restricts the requested scope. Preview displays all submitted levels, difficulty
counts, total, duration and negative marking. Explicit confirmation and a final
Create action are required. Zero inventory disables continuation and never causes
an automatic fallback to the whole course.

Existing ExamCreation receipts, request keys, response parsing, own-practice assignment,
and participation/result paths remain in use. Completed or uncertain creations are
not sent again to Quiz24. Received responses can still finish local persistence on
retry without requiring another inventory fetch. Participation retains the existing
verified-mobile and synchronized Quiz24 identity guard.

No schema changes or production migrations are needed. No live Quiz24 mutations were
used during implementation: tests mock the provider and browser API.

Targeted checks:

```sh
DJANGO_SETTINGS_MODULE=apps.exams.tests.settings python manage.py test apps.exams.tests.test_question_bank apps.exams.tests.test_student_practice apps.exams.tests.test_builder apps.exams.tests.test_integration apps.planning.tests.test_flexible
npm run test:question-bank
npm run test:question-bank:browser
npx tsc --noEmit
```

Browser checks require installed Chrome (`CHROME_BIN` can override the path).

Validation on 2026-10-21: targeted backend tests passed on PostgreSQL and SQLite,
including authorization, exact scope, inventory and creation-receipt recovery.
Frontend unit tests and the mobile Chrome flow use recorded real catalog data.
No migrations or live provider mutations were performed.

## Recorded-contract regression checks

`tests/fixtures/automatic_catalog.json` records read-only Quiz24 catalog, hierarchy
and difficulty inventory responses captured on 2026-10-21. The student-specific
frontend fixture records the actual authorized API responses. Course 109 is shared
across several grade/field rows; this is valid and must not trigger a missing-ID error.
On identifier/path validation failure the backend refreshes the authoritative cache
once and revalidates the full scope; invalid selections remain rejected intact.
Tests in `test_automatic_catalog` use real DRF routes and recorded responses, with
all createOnlineExam calls mocked. Real read-only counts succeeded at course, chapter,
subject and object depth. No live createOnlineExam was authorized or performed.

```sh
python manage.py test apps.exams.tests.test_automatic_catalog
```
