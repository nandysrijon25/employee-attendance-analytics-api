# REVIEW.md — Starter Code Review

| # | Where | What was wrong | How to notice | Fix in the revised implementation |
|---|---|---|---|---|
| 1 | `health()` | It returned `200` without checking MongoDB readiness. | Stop MongoDB and call `GET /health`; the starter still reports healthy. | Ping MongoDB and return `503` when unavailable. |
| 2 | MongoDB setup | No required indexes were created at startup. | Inspect `getIndexes()` or run concurrent duplicate requests. | Create named indexes idempotently at startup, including unique employee and attendance keys. |
| 3 | `create_employee()` | Duplicate detection used check-then-insert, which races under concurrent requests. | Send simultaneous creates for the same `emp_code`. | Enforce uniqueness with a database unique index and translate `DuplicateKeyError` to `409`. |
| 4 | `create_employee()` / `EmployeeIn` | Employee fields and shift/date values were not validated. | Submit blank fields, malformed dates/times, or equal shift times. | Add request validation for field formats, date, and distinct shift start/end. |
| 5 | `create_employee()` | `created_at` used a naive server-local datetime. | Run the server in a different time zone and compare returned instants. | Store a UTC datetime and serialize instants as epoch milliseconds. |
| 6 | `list_employees()` | `skip = page * page_size` skipped the first page. | Request page 1 and compare with the first sorted records. | Use `(page - 1) * page_size`. |
| 7 | `list_employees()` | `total` counted the entire collection, not the filtered department. | Filter by a department and compare `total` with matching records. | Count using the same query filter as the list. |
| 8 | `list_employees()` | Results were not sorted by `emp_code`, and pagination bounds were not validated. | Repeat requests and pass page/page_size values outside the contract. | Sort by `emp_code` and constrain page and page size. |
| 9 | `compute_late_minutes()` | It replaced the timestamp's hour using its existing timezone/date and used integer minutes before applying the grace rule. This mishandles IST boundaries and the exact `10:00:01` case. | Test 09:40:00 and 09:40:01 for a 09:30 shift, plus UTC/IST date boundaries. | Convert the instant to IST, compare to the attendance-date shift start, apply the strict 10-minute grace rule, and floor elapsed minutes. |
| 10 | `compute_work_hours()` | Python `round()` uses floating-point/banker's rounding rather than the contract's decimal half-up rounding. | Test values exactly around a two-decimal rounding boundary. | Use `Decimal` and `ROUND_HALF_UP`. |
| 11 | `compute_overtime()` | It did not support overnight shifts and did not apply the minimum 30-minute overtime threshold. | Test a 22:00–06:00 shift and a punch-out 29 versus 30 minutes after shift end. | Calculate the correct next-day shift end for overnight shifts and count overtime only at 30 minutes or more. |
| 12 | `punch_in()` | It did not return `404` for an unknown employee before using employee shift fields. | Punch in with an unknown `emp_code`. | Check employee existence and return `404`. |
| 13 | `punch_in()` | It used server-local time and treated `punched_at=0` as if no timestamp was supplied. | Submit a bad/seconds timestamp or compare time-zone boundary cases. | Validate integer epoch milliseconds, distinguish omitted from supplied values, truncate to seconds, and convert at the API boundary. |
| 14 | `punch_in()` | It used a check-then-insert duplicate guard, accepted arbitrary statuses, and returned an `id` not defined by the contract. | Send concurrent punch-ins, an invalid status, or inspect the response keys. | Accept only presence statuses, rely on the unique `(emp_code, date)` index, return `409` on duplicate, and never expose Mongo `_id`. |
| 15 | `punch_in()` | Attendance date and late calculations did not implement the IST/overnight shift rules. | Test an overnight-shift punch-in shortly after midnight IST. | Derive the attendance date using the employee's shift and compute lateness against that shift date. |
| 16 | `list_attendance()` | It loaded all matching documents into Python before sorting and paginating. | Increase the collection size and inspect memory/query time. | Apply indexed filtering, sorting, skip, and limit in MongoDB; count with the same filters. |
| 17 | `list_attendance()` | It returned Mongo `_id` as `id` and left stored datetimes in their internal format. | Compare output keys/types with the API schema. | Exclude `_id`; serialize punch/history instants as epoch milliseconds and supply documented defaults for legacy fields. |
| 18 | API coverage | Punch-out, regularization, four analytics endpoints, and the explain endpoint were absent. | Compare registered routes with `openapi.yaml`. | Add the missing routes and build the analytics using MongoDB aggregation pipelines. |

## Reviewed and retained

- `emp_code` is the business identifier; MongoDB `_id` must remain internal.
- `history` is reserved for manual corrections; punch-in and punch-out must not append history entries.
- Analytics must use stored derived fields and aggregation pipelines rather than recomputing values in Python.
- The unique attendance key is `(emp_code, date)`, allowing one attendance record per employee per attendance date.

This review records defects found in the original starter and the corresponding intended fixes in the revised `app/main.py`. The revised implementation still needs full contract testing against the evaluator's MongoDB version and dataset; a passing local happy path alone is not proof of complete correctness.
