# REVIEW.md

Comprehensive code review findings for the original starter `app/main.py` implementation.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` (Line 72) | Health probe returns `200 OK` unconditionally without checking database reachability. | Stop MongoDB process; `GET /health` still returns 200 instead of 503. | Added `db.command("ping")` within try-except, raising `HTTPException(503)` on failure. |
| 2 | `create_employee()` (Line 77) | Uses non-atomic `find_one` check-then-insert pattern; permits identical `shift_start` & `shift_end`; stores naive `datetime.now()` for `created_at`. | Simultaneous `POST /employees` with identical `emp_code` both succeed; equal shift start/end accepted. | Created unique index `{emp_code: 1}`, catch `DuplicateKeyError` returning 409; added shift validation in Pydantic; formatted `created_at` as epoch ms. |
| 3 | `list_employees()` (Line 88) | Missing sorting by `emp_code` ascending; broken pagination skip formula (`skip = page * page_size`); `total` ignores department filter. | Page 1 skips first 20 records; filtering by department returns total count of all employees. | Added `.sort("emp_code", 1)`, fixed skip formula to `(page - 1) * page_size`, passed filter `q` to `count_documents(q)`. |
| 4 | `punch_in()` (Line 99) | Non-atomic `find_one` duplicate check; ignores timezone offsets and overnight shift date logic (R1); late minutes grace period boundary broken; returns internal `id`. | Concurrent punch-ins both create records; punch-in at 09:40:00 counted as late; schema contains extra `id` field. | Created unique compound index `{emp_code: 1, date: 1}` returning 409 on race; enforced strict > 10 min grace period logic; formatted response per contract. |
| 5 | `list_attendance()` (Line 124) | Loads entire collection into Python memory using `list(db.find(q))` and sorts in Python; sort missing `emp_code` ascending; missing `date_from > date_to` validation. | High memory consumption and latency on ~100k docs; `date_from > date_to` returned 200 instead of 422. | Performed `.sort([("date", -1), ("emp_code", 1)]).skip().limit()` directly in MongoDB query; added 422 check for invalid date range. |
| 6 | `compute_late_minutes`, `compute_work_hours`, `compute_overtime` (Lines 33–47) | Naive `.replace(hour=h, minute=m)` datetime math fails across midnight and timezones; standard float division causes floating-point rounding errors. | Overtime for overnight shift calculated against wrong day's shift end; work hours float inaccuracies. | Rewrote helpers to convert instants to IST, account for overnight date offsets, and perform Decimal `ROUND_HALF_UP` calculations. |

## Investigated Non-Defects

1. **Environment Loading Strategy (`load_dotenv()` vs `os.getenv`)**:
   - Investigated whether `load_dotenv()` would override environment variables in production.
   - Confirmed: Standard `load_dotenv()` loads local `.env` values only if system environment variables are not already set, satisfying the requirement that real environment variables win.

2. **MongoDB BSON Date Storage vs Epoch Milliseconds API Boundary**:
   - Investigated storing integer epoch milliseconds directly in MongoDB vs BSON datetimes.
   - Decision: Stored BSON datetimes in MongoDB as required by `DATA_MODEL.md` (which enables native Mongo date operators) while converting to integer epoch milliseconds at the FastAPI boundary.
