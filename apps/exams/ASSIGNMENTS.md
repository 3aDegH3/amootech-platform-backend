# Local exam assignments (Sprint 1)

`ExamAssignment` links one shared `Exam` to students. The unique exam/student
constraint makes repeated assignment idempotent. `Exam.student` remains the
legacy primary student; safe removal clears it if needed. Migration 0005
backfills all existing non-null primary students without changing results.

Only the exam's owning counselor can use:

- `GET /api/v1/exams/{id}/assignments/`: paginated current-responsibility students,
  with `student`, `student_name`, and `assigned_at`.
- `POST /api/v1/exams/{id}/assignments/`: assign locally.
- `DELETE /api/v1/exams/{id}/assignments/`: remove before participation.

POST/DELETE accept `{"student_ids": [1, 2]}` (maximum 200 entries), validate the
whole batch, and return `assignment_count`. Invalid or unauthorized IDs return
400 without partial changes. Any attempt blocks removal with 409. An authorized start intent
creates one pending attempt per exam/student before provider HTTP; started_at is
set only after the final authorized link response. Its lease fences concurrent
starts; completed attempts cannot request unsupported retakes. Assignment operations do
not contact Quiz24 or create external exams.

Counselors cannot manage personal practice or see student metadata/results
after that student moves to another counselor. Historical assignments/results
remain intact; the new counselor cannot access the previous counselor's exam.
Students see published/closed assigned exams and their own personal exams;
participation and result sync always use the authenticated student's identity.
Existing counselor/admin result routes retain legacy primary-student behavior.
Per-assignee counselor result selection and participant UI belong to later work.
No new assignment-management privilege is granted to admin/supervisor roles.

Assignments enforce Amootech access only. Direct Quiz24 link restrictions and
provider-side assignment enforcement are unverified. No provider mutations,
scheduling, notifications, or analytics are introduced.

Apply after deployment review, from the backend directory:

```sh
python manage.py migrate exams
```

Focused checks:

```sh
python manage.py test apps.exams.tests --settings=apps.exams.tests.settings
python manage.py makemigrations --check --dry-run --settings=apps.exams.tests.settings
```

Tests use an isolated SQLite database and mocked provider calls. PostgreSQL
concurrency was verified on an isolated PostgreSQL 16 cluster in Sprint 7,
including simultaneous assignment additions and removal during start. See
[QA_SPRINT7.md](QA_SPRINT7.md); hosted-link behavior remains unverified.
