# Shared exam builder — Sprint B

The student bank, student report builder and counselor automatic builder all mount
`src/components/exams/ExamBasketBuilder.tsx`. `selectionBasket.ts` owns explicit
course/topic intent, exact ancestor filters, per-course difficulty counts and report
references. Its versioned sessionStorage key includes authenticated user, mode and
sorted counselor recipient IDs. Switching the active grade/course changes browsing,
not the entries map. Only explicit removal or topic refinement changes a saved scope.
Errors leave that map intact. No provider inventory or access decision is trusted
from browser storage; the server revalidates both before creation.

The backend reuses `/provider/{grades,courses,course-detail,question-count}/`,
`/study-sources/topics/`, scoped report preview and `/automatic/`. `services/builder.py`
checks the local publication and each owned recipient's period/field. Creation checks
each recipient's verified mobile and Quiz24 school link before inventory and again
before claiming a remote mutation. A foreign recipient or unsupported group member
fails the entire request. Counts remain advisory until the final creation check.

`ExamCreation.confirmed_scope.selection` stores actual provider filters, labels,
verified topic paths, quantities, settings and recipient IDs. Report creation also
stores the original source snapshots and per-source topic links. Existing receipt
states and atomic claims are reused: a successful response with failed local
persistence can finish locally; SENDING/UNKNOWN never blindly repeat remote creation.
Existing assignments, scheduling, start links, result synchronization and management
continue to use the existing Exam services. Exam lists have deterministic date/ID order.

## Official multi-course verdict: UNVERIFIED

Checked read-only on 2026-10-26: https://www.quiz24.ir/WebService/v1

- `createOnlineExam` accepts one gradeId/courseId and one global duration.
- `createExam` returns a hosted editor URL, with one gradeId. It does not accept the
  basket or per-course automatic question distribution.
- `editExam` returns an editor URL for one existing examId.
- The published contract does not establish an automatic question aggregation or
  merge operation that can turn this basket into one online exam.

The provider's web editor is not proof of programmatic support. The UI preserves
all courses, explicitly disables combined submission, and offers an individually
labelled single-course creation button. There is no batch creation disguised as a
single exam. Automatic creation and count serializers reject unknown/multi-course
payloads. An official supported aggregation contract is still needed before enabling
one exam identity, participation link, duration and result flow across courses.

## Catalog and report policy

Browsing and ancestry validation are entirely local. The five observed orphan
courses (4929, 4913, 4914, 4919, 4920) remain in quarantine outside active envelopes
and node counts. This is missing-parent validation, not a permanent blacklist of
grades 122/206. Historical references are not deleted or remapped.

Report groups contain only explicit COMPLETED/PARTIAL occurrences. They expose
verified local descendants of saved scope, without prechecking any studied topic.
Duplicate topics retain all authorized report references. Unverified legacy context
has a separate explicit reselection action; titles never become provider IDs.
Topic checkboxes and expanding a parent are independent. Disjoint mixed depths are
rejected when the provider cannot represent their exact union.

## Verification and prerequisites

No new model or migration is introduced by this follow-up. Existing academics
0004–0007 and exams 0011 must be applied; the existing Sprint A planning migration
is also present in the tested graph. A published local catalog is required.
The development .env names Docker host `db`; no Compose services were running during
this audit, so its database/catalog state could not be inspected. It was not modified.

For a newly initialized disposable UTF-8 PostgreSQL cluster ONLY:

```sh
qa_pg_dir=$(mktemp -d /tmp/amootech-sprint7-pg.XXXXXX)
/usr/lib/postgresql/16/bin/initdb -D "$qa_pg_dir/data" --auth-local=trust --auth-host=reject --no-locale -E UTF8
/usr/lib/postgresql/16/bin/pg_ctl -D "$qa_pg_dir/data" -l "$qa_pg_dir/server.log" -o "-k $qa_pg_dir -c listen_addresses= -p 55437" -w start
EXAM_TEST_PG_SOCKET="$qa_pg_dir" .venv/bin/python manage.py test apps.academics.tests_local_catalog apps.exams.tests apps.accounts.tests apps.daily_reports.tests apps.core.tests apps.planning.tests --settings=apps.exams.tests.postgres_settings --noinput
EXAM_TEST_PG_SOCKET="$qa_pg_dir" .venv/bin/python manage.py makemigrations --check --dry-run --settings=apps.exams.tests.postgres_settings
EXAM_TEST_PG_SOCKET="$qa_pg_dir" .venv/bin/python scripts/sprint_b_qa.py --session "$qa_pg_dir/session.json" --serve
```

The QA script rejects mismatched PostgreSQL data directories before any migration,
loads only recorded provider taxonomy, links synthetic students, and forbids live
HTTP. Only provider inventory, creation and participation transport are mocked;
Django authorization, API calls, migrations and persistence are real PostgreSQL.
It is intended to run once in a fresh disposable cluster, not to reset existing data.
From the frontend, while that isolated server is running:

```sh
SPRINT_B_QA_SESSION="$qa_pg_dir/session.json" npm run test:sprint-b:browser
npm run test:exam-basket
node --experimental-strip-types --test tests/*exam*.test.mjs tests/question-bank.test.mjs tests/report-topics.test.mjs tests/jalali-time.test.mjs tests/selection-basket.test.mjs
npx tsc --noEmit --incremental false
npm run lint
```

Then stop the QA server and disposable PostgreSQL cluster. Do not use developer or
production DB credentials for the test commands. No live provider creation, commit,
push or deployment was performed. See the repository-level Persian Sprint B report
for measured results and remaining acceptance limits.
