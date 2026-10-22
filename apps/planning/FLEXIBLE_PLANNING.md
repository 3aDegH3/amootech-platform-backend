# Flexible planning foundation

The existing `/api/v1/planning/items/` create/PATCH/read routes remain in use.
Existing `subject`, `chapter`, and `topic` fields refer only to internal academics.
They may be omitted, including for STUDY/TEST/REVIEW. Existing duration and test-count
rules and execution/report edit locks still apply.

For Quiz24, send `taxonomy` as an object, for example:

```json
{"provider": "quiz24", "grade": 700, "course": 900, "object": 903}
```

IDs are positive integers from the provider, not internal academic primary keys.
Every level is optional; a descendant needs a course context so its ancestry can
be verified against courseDetail. Selected ancestors must match, but intermediate
levels need not be selected. Course-only and grade-only selections are valid.
Internal FKs and a Quiz24 selection cannot be combined on one item. To switch
namespaces clear the old selection in the same PATCH (`taxonomy: {}` or null FKs).

Read responses retain selected IDs and backend-resolved `taxonomy.labels` separately
from `title`. Client-supplied labels are replaced with catalog labels. The adapter
uses existing grades/courses/courseDetail methods, cached for 15 minutes per provider
configuration. Missing or malformed selections fail validation; free-text items
remain available without calling Quiz24. No exam creation methods are involved.

`generated_title` uses the course and deepest selected label, omitting duplicate
names. Grade-only falls back to the grade label. Internal chapter/topic-only input
can derive the course through its existing ancestors. A nonblank submitted `title`
is a manual override, reported by read-only `title_is_custom`. Subsequent taxonomy
edits preserve it. Send `reset_generated_title: true` to replace it with the generated
title; this write-only flag cannot accompany a title. Generated titles track taxonomy
edits. Historical rows default to custom, preserving existing titles verbatim.

`Grade.number` is explicit canonical metadata (1–12). The data migration recognizes
only the twelve exact Persian canonical grade names, leaving ambiguous names unset.
An admin can set unknown grade metadata through the existing grade API. Grade IDs,
provider IDs and ordering are never interpreted as grade numbers.

The reusable policy scopes authenticated students to 1–6, 7–9 or 10–12. Internal
upper-secondary fields match the student's field name across grades. Provider
grades match canonical catalog labels; provider courses must reference an allowed
grade. Upper-secondary provider access additionally requires explicit matching
`fieldName`; missing field metadata is denied. No mapping is guessed. Invalid or
missing student grade metadata returns 403, including before provider calls.
The policy covers academic list/detail/tree and provider grades/courses/course-detail/
question-count. Existing anonymous grade/field access for onboarding remains available.
Counselors browse the catalog but existing ownership checks still govern plans and
student-specific tree requests. Exam mutations/discovery workflows are outside this sprint.

Apply migrations with `python manage.py migrate`. No historical item is replaced,
renumbered or inferred from title text. New metadata is also retained by day/plan copies.
