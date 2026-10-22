# Quick student daily reports

Use the existing student-owned endpoint:

`PUT /api/v1/daily-reports/<report-id>/planned/<plan-item-id>/`

```json
{"reported_status": "COMPLETED"}
```

The three declarations are `COMPLETED` (خوندم), `PARTIAL` (نصفه), and
`NOT_DONE` (نخوندم). `DailyReportItem.reported_status` is nullable: NULL means
UNSET. Opening a day and migrating historical rows do not infer a declaration
from measurements or an execution record. Existing `completion_status` inputs
retain their prior execution behavior; a request cannot contain both inputs.

A quick declaration modifies only that report row's status/source. It does not
start, stop or rewrite a timer, copy planned minutes/test counts, change titles or
taxonomy, or infer exact topic coverage. Optional metrics and notes remain intact.
Running timers remain visible in optional details and must still be stopped before
closing the day through the existing workflow. Explicit declarations take precedence
in completion summaries; actual measurements retain their existing source priority.
The existing topic repetition calculation receives no new execution or measurement
from a quick declaration.

Read payloads add `reported_status`, contextual `taxonomy`, `editable`, and
`reporting_progress` (reported/total planned rows, including events). The existing
`execution_status` field continues to describe execution rather than declaration.
Daily progress in the student UI counts confirmed declarations, never syllabus
coverage or completion percentage. The client serializes full-day mutation responses
and guards duplicate taps; errors preserve the previously confirmed selection.

Existing ownership, published-plan/date validation, row uniqueness, and Tehran
calendar rules remain enforced in Django. Status writes lock owner/report/row in
that order. Current and past 30-day reports remain editable; older existing reports
remain readable. Opening an editable past report adds missing published activities
without replacing its existing rows.

Migration `0005_dailyreportitem_reported_status` adds only a nullable column, with
no data rewrite. Apply with `python manage.py migrate` during deployment. It was
exercised against an isolated test database, not production.

Focused checks:

- Backend: `python manage.py test apps.daily_reports.tests apps.planning.tests.test_execution`
- Frontend: `npm run test:reports`, `npm run test:reports:browser`, `npx tsc --noEmit`
- Browser test uses local mock API and installed Chrome (`CHROME_BIN` can override its path).
