# Group results and counselor analytics (Sprint 5)

Source contract: https://www.quiz24.ir/WebService/v1#examResult
The public documentation describes Score/Balance as Double, Rank/RankInClass
and Correct/Wrong/NoAnswer as Integer. Score is "total score"; its scale/range
is unspecified. Therefore no percent-score conversion, 0–100 bands, fabricated
Balance or local standardized/taz index is implemented. Provider ScoreAvg,
BalanceAvg and TotalParticipation describe another population and are not used
as Amootech group aggregates. Provider payloads were mocked; no live API calls.

## Population and calculations

All metrics are restricted to currently assigned students still under the
owning counselor. Personal practice is excluded from counselor group access.
Completed count uses existing COMPLETED participation/result evidence; a URL
issuance/PENDING attempt is not completion. Participation percentage is explicitly
completed / assigned * 100, not a claim about verified starts. Empty denominator
returns null.

A comparable score requires a COMPLETED attempt, a successfully parsed overall
raw Quiz24 Score, the same exam code, and a matching verified identity binding
(current synchronized external account, school, mobile and verification time).
Legacy results remain intact but require re-verification before group scoring.
There is no name matching. New results use the existing supported examResult
request for the verified mobile username of each eligible completed student.

Mean, median, min, max and distribution include comparable results only; missing
scores are never zero. Genuine provider Score=0 and negative scores are valid.
Five equal-width bins span observed min/max, with lower-inclusive/upper-exclusive
bounds except the final upper-inclusive bin. Identical scores produce one bin.
Balance average includes only available finite provider Balance from comparable
results; the contributor count is returned. Correct/wrong/unanswered totals and
averages have independent contributor counts and the same scored population.

Amootech group rank sorts actual raw score descending and uses competition ties
(1,1,3). It is distinct from provider Rank and RankInClass. All ranks are
provisional because membership, completions and provider results can change.
Search/filter/page selection does not change the statistics or rank population.
Stale verified caches are preserved and labelled, not silently refreshed or
zeroed. Five-minute freshness expires locally in the UI without API polling.

## APIs

Counselor owner only (no additional admin/supervisor/student privileges):

- GET /api/v1/exams/{id}/analytics/?q=name&status=SCORED&ordering=rank&page=1
- POST /api/v1/exams/{id}/analytics/sync/
- GET /api/v1/exams/{id}/result/?student={authorized-assignee-id}

Analytics GET returns DRF count/next/previous/results, metrics, score_scale,
ranking, last successful sync, batch/freshness/partial indicators. Status filters:
SCORED, IDENTITY_MISSING, NEEDS_VERIFICATION, AWAITING_RESULT, NOT_COMPLETED.
Ordering: rank (score descending), name, score_asc. Rows omit answers/raw provider
bodies and include separate group/provider/class rank values. Drill-down reuses
the existing detailed result serializer with stored answers and current student
responsibility/assignment checks; it is a GET, not another provider sync route.

Sync has a fenced 90-second lease, 60-second cooldown, at most ten completed
students per request, at most 15 seconds per provider call and a 30-second total
network budget. A persisted assignment cursor rotates through eligible students;
remaining_in_cycle and batch outcomes tell the counselor when another manual
batch is needed. No workers/polling/creation/participation calls are introduced.
Each successful student's response commits in a short transaction, rechecking
exam ownership, identity and assignment membership. HTTP is outside database
transactions. Partial success is explicitly reported, not silently atomic.
Incomplete/failed responses leave the corresponding verified result and answers
unchanged. Repeated successful sync updates the unique exam/student attempt.

Group sync stores supported examResult answers when available. Existing stored
answers are preserved if the response lacks valid answers; bulk sync does not
make additional answersheets calls. Legacy individual result sync retains its
answersheets integration and now performs HTTP outside transactions, with a
second release/identity/ownership check before saving. Group drill-down reads
existing details only.

## Persistence and security

Migration 0008 adds optional provider_rank_in_class and result verification
metadata to ExamAttempt, plus ExamAnalyticsSync for leases/cooldown/cursor.
Existing attempts/answers/results are preserved and not falsely backfilled as
verified. No new result/answer copies or raw participant lists are stored.

Students cannot access analytics endpoints or select another student's result.
Existing result_release_at still guards their own scores/Balance/answers before
any provider call and is rechecked before persistence. Group metadata/ranks are
not added to student endpoints. Counselor management visibility remains scoped
to current responsibility. Hosted Quiz24 URLs can bypass local release policy;
this implementation does not claim to restrict them.

Run scoped Django exams/frontend tests and TypeScript/ESLint. PostgreSQL lease
concurrency regressions skip on SQLite and passed on an isolated PostgreSQL 16
cluster in Sprint 7; see [QA_SPRINT7.md](QA_SPRINT7.md).
Provider response nesting beyond a direct overall summary (or singleton summary
list) fails closed and requires verified contract samples before expansion.

Apply after deployment review from the backend directory:

```sh
python manage.py migrate exams
```

Sprint 7 also protects verified personal results from incomplete refreshes and
preserves stored answers on empty/missing answer collections. Ambiguous overall
summaries or per-course scores are not promoted to an overall result; naive
provider timestamps are not assigned a guessed timezone.
