# Review of the original starter

Locations refer to function names in the original starter. A comparison copy was retained locally and excluded from this submission.

| # | Where | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | MongoClient configuration | Reads naive BSON datetimes, leaving their timezone ambiguous. | An instant changes interpretation across host timezones. | Use timezone-aware UTC reads. |
| 2 | Startup | No unique employee index. | Parallel duplicate creates can both succeed. | Unique emp_code index, created at startup. |
| 3 | Startup | No unique attendance index. | Parallel punch-ins create two records for one day. | Unique compound emp_code/date index. |
| 4 | Startup/list endpoints | No query indexes beyond _id. | Lists scan the collection as it grows. | Index employee department/code and attendance date/code, status/date/code, employee/status/date and employee/punch_in. |
| 5 | health | Reports ready without checking MongoDB. | Stop MongoDB and health still returns 200. | Ping; return 503 on driver errors. |
| 6 | EmployeeIn | No employee-code format validation. | EMP1 is accepted. | Require EMP followed by 4–6 digits. |
| 7 | EmployeeIn | Names, email and department lack bounds/format checks. | Empty name or malformed email succeeds. | Apply contract lengths and email pattern. |
| 8 | EmployeeIn | Shift strings are unchecked, including equal times. | 25:00 or equal start/end succeeds. | Validate HH:MM and reject equal times on creation. |
| 9 | EmployeeIn | joined_on is an arbitrary string. | 2026-02-30 succeeds. | Check exact date syntax and calendar validity. |
| 10 | create_employee | Check-then-insert is not concurrency-safe. | Two creates both pass find_one. | Insert directly and map DuplicateKeyError to 409. |
| 11 | create_employee | datetime.now uses naive host time. | UTC and IST hosts store different instants. | Use aware UTC server time, truncated to seconds. |
| 12 | Employee/attendance responses | Datetimes are returned as strings instead of epoch milliseconds. | created_at/punch_in are JSON ISO strings. | Recursively serialize dates to integer milliseconds, including nested audit values. |
| 13 | list_employees | skip = page * page_size skips the first page. | page=1 omits the first 20 employees. | Use (page-1)*page_size. |
| 14 | list_employees | total ignores department filter. | Two departments produce the same unfiltered total. | Count using the applied query. |
| 15 | list_employees | No deterministic emp_code ordering. | Insert EMP0003 before EMP0001. | Sort in MongoDB by emp_code ascending. |
| 16 | Both list endpoints | Invalid page sizes and page numbers accepted. | page=0 or page_size=101. | Enforce page>=1 and 1<=page_size<=100. |
| 17 | PunchInIn | Timestamps are coerced and unchecked. | Strings, floats, seconds and explicit null accepted. | Strict integer milliseconds, contract bounds; omitted timestamp defaults to now. |
| 18 | PunchInIn | Any status accepted. | Punch-in with ABSENT. | Restrict punch-in to PRESENT, WFH, ON_DUTY. |
| 19 | punch_in | Unknown employee is dereferenced. | Unknown emp_code produces 500. | Return 404 before calculating. |
| 20 | punch_in | Local-host timestamp conversion determines attendance date. | UTC 20:00 belongs to the next IST date. | Convert input to UTC, derive the calendar date in IST. |
| 21 | punch_in/helpers | Overnight shifts not handled. | 01:00 punch-in for 22:00–06:00 is assigned to the wrong day. | Use R1 previous-day rule and next-day shift end. |
| 22 | punch_in/helpers | Milliseconds are not truncated before calculation/storage. | 09:40:00.900 is not returned at whole seconds. | Truncate instants before calculations. |
| 23 | compute_late_minutes | Floors minutes before testing the grace threshold. | 09:40:01 for 09:30 shift incorrectly gives 0. | Compare seconds >600, then floor minutes from shift start. |
| 24 | compute_late_minutes | Replaces the hour in whichever timezone the input uses. | UTC punch at 04:35 compared to 09:30 UTC instead of IST. | Construct shift bounds explicitly in IST on the attendance day. |
| 25 | compute_work_hours | Uses binary float/banker's rounding. | 1 hour 18 seconds must round 1.005 to 1.01. | Decimal arithmetic with ROUND_HALF_UP. |
| 26 | compute_overtime | Counts overtime below the 30-minute minimum. | 18:35 out after 18:30 end gives 5. | Return zero below 30 whole minutes. |
| 27 | compute_overtime | Always places shift end on the same calendar day. | Overnight punch-out has almost a day of spurious overtime. | Resolve overnight end on the next day. |
| 28 | punch_in | Duplicate attendance check is racy. | Parallel requests both pass find_one. | Unique index and DuplicateKeyError handling. |
| 29 | punch_in/list_attendance | Exposes MongoDB IDs as id. | Response contains an extra id field. | Keep MongoDB IDs internal; use emp_code/date. |
| 30 | list_attendance | Loads and sorts all matching documents in Python. | Memory and latency rise with 100,000 rows. | MongoDB sort/skip/limit and count_documents. |
| 31 | list_attendance | No employee tie-breaker for the same date. | Same-day records have unstable pagination. | Sort date descending, emp_code ascending. |
| 32 | list_attendance | Date/status filters and reversed ranges are unchecked. | Invalid status/date or date_from>date_to. | Validate enums, real dates and range ordering. |
| 33 | list_attendance | Missing legacy fields remain missing. | Seeded legacy row lacks history/half_day. | Apply documented response defaults without recalculating stored values. |
| 34 | List filter construction | Truthiness silently discards explicitly empty filters. | department= returns every employee. | Distinguish omitted (None) from supplied empty string. |

## Examined and retained

- `load_dotenv()` already preserves real environment variables by default; made `override=False` explicit.
- Synchronous FastAPI handlers with synchronous PyMongo are appropriate: FastAPI runs ordinary handlers in its worker thread pool.
- emp_code as the employee identifier and strings for attendance dates/shift times match the model.
- Null work_hours for an open record, empty history at punch-in and ignored unknown/derived request fields match the contract.
- The original attendance pagination formula was correct, although it ran in memory.
- The inclusive date bounds were correct; validation was missing.
- Punch-out and corrections were missing features, not claimed as defects in existing implementations. They now share the calculation helpers; corrections atomically append history and reject stale snapshots.

## Contract interpretation

- R1 includes equal shift times in overnight calculations, but POST /employees explicitly forbids equal times. Creation rejects them; calculations support seeded equal-time shifts.
- The regularization schema says derived fields cannot be sent, while the general convention explicitly says to ignore them. Unknown/derived fields are ignored; a resulting no-op correction is 422.
- Requests omit optional timestamps to keep/default values. Explicit null is rejected because request schemas are non-nullable; response punch times can be null.
- Punch-out selects the latest punch_in at or before punched_at. If the only punch-in is later, the selection finds nothing and returns 404; equality returns 422. The prose also says <= is invalid, but does not explain selecting a later record. This implementation follows the explicit selection rule.
- 'No endpoint creates ABSENT or LEAVE records' means no new record is inserted with these statuses; PATCH can change an existing record to either, as explicitly permitted.
