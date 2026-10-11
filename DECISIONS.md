# DECISIONS.md

1. **Indexes.**
   Created `{ emp_code: 1 }` (unique) on `employees` to enforce code uniqueness. Created `{ department: 1, joined_on: 1 }` on `employees` for department trend and headcount filtering. Created `{ emp_code: 1, date: 1 }` (unique) on `attendance_logs` to guarantee at most one record per employee per day and block concurrent duplicate punch-ins. Created `{ date: -1, emp_code: 1 }` on `attendance_logs` to optimize paginated list queries. Considered and rejected an index on `{ status: 1 }` alone due to low cardinality (5 status values), making compound date indexes far more selective.

2. **Punch-in race.**
   The database unique compound index `{ emp_code: 1, date: 1 }` acts as the atomic barrier. When two identical requests arrive simultaneously, MongoDB allows exactly one insert to succeed (201 Created). The second concurrent insert violates the unique constraint and raises `DuplicateKeyError`, which the API catches and returns 409 Conflict.

3. **Ties.**
   The leaderboard uses standard competition ranking (`$rank` in `$setWindowFields`). Employees tied on late minutes share the same rank (e.g., 1, 2, 2, 4). Filtering by `limit` applies *after* ranking (`rank <= limit`), so all employees tied at or below the cutoff are returned.

4. **Headcount.**
   Department summary queries `employees` first (filtering by `joined_on <= month_end`), then `$lookup` joins `attendance_logs` using `$unwind` with `preserveNullAndEmptyArrays: True`. Employees with zero logs yield null log fields but count toward department headcount.

5. **100x Data.**
   Shard `attendance_logs` by `(department, date)` and maintain pre-aggregated daily summary collections updated asynchronously on punch-in and punch-out events.
