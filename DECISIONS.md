# Design decisions

1. **Indexes.** Unique employee emp_code and attendance (emp_code, date descending) indexes enforce identity and daily uniqueness; they also serve joins and monthly lookups. Employee (department, emp_code) supports department filters and sorting; (joined_on, department) serves eligible headcount. Attendance (date descending, emp_code), (status, date descending, emp_code), and (emp_code, status, date descending) support list filters and ordering; the date index also serves leaderboards. (emp_code, punch_in descending) finds the latest punch. I rejected a standalone status index: compound indexes already cover it and support ordering.

2. **Punch-in race.** Each request validates the employee and computes the same attendance date. Both attempt insertion. MongoDB's unique compound index permits exactly one insert; the winner returns 201, and DuplicateKeyError becomes 409 for the loser.

3. **Ties.** MongoDB ranks totals before filtering rank <= limit. Equal totals share a rank, so cutoff ties all appear; later ranks skip positions. Employee code orders tied rows deterministically.

4. **Headcount.** Department summaries start with eligible employees and look up their monthly totals. Empty lookups contribute zero metrics but still one person.

5. **100x data.** I would maintain daily department summaries with correction-aware updates, reducing repeated trend aggregation while retaining raw logs for audits.
