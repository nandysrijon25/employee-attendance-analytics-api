# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` so duplicate employee codes cannot be inserted concurrently. A unique `(emp_code, date)` index on attendance guarantees one record per employee per attendance date. Department/employee and department/join-date indexes support department lists and headcount queries. Attendance indexes support date sorting, employee history, and late-minute queries. I would avoid adding indexes without a measured query need because each index adds write and storage cost.

2. **Punch-in race.** Both requests may pass the employee lookup, but the unique attendance index decides the race at insert time. One insert succeeds and returns `201`; the other receives a duplicate-key error and returns `409`. A check-then-insert alone would not be safe.

3. **Ties.** The late leaderboard uses MongoDB ranking by total late minutes. Employees tied at the limit share the same rank, and the next rank is skipped (for example, `1, 2, 2, 4`). Filtering is by rank, so all employees tied at the cutoff are returned. The final display order uses `emp_code` for stable tie ordering.

4. **Headcount.** The department summary starts from employees and looks up attendance records. That keeps employees with no logs in the group. It excludes people who joined after the month's last day.

5. **100x data.** I would measure explain plans and query latency first, then review compound indexes, connection pooling, and aggregation costs with representative data. I would also add automated integration and concurrency tests before optimizing further.
