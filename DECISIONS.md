# DECISIONS.md

Architectural and design decisions for the Employee Attendance & Analytics API.

---

### 1. Indexes and Query Plan Strategy

To achieve sub-millisecond lookups and eliminate collection scans across all endpoints, the following indexes are created idempotently at application startup via FastAPI's lifespan hook:

1. **`employees` Collection:**
   - `emp_code` (Unique): Enforces employee identifier uniqueness and provides $O(1)$ index lookups for employee retrieval and punch-in/out validation.
   - `department`: Optimizes filtering by department and supports department-scoped ranking and summaries.
   - `joined_on`: Filters employees active by the given cutoff date (`joined_on <= month_end`), ensuring efficient headcount discovery without scanning subsequent joiners.

2. **`attendance_logs` Collection:**
   - `(emp_code, date)` (Unique, Compound): Enforces the core business rule that an employee can have at most one attendance record per IST calendar date. It also provides high-selectivity prefix lookups for employee monthly summaries and regularizations.
   - `(date DESC, emp_code ASC)` (Compound): Optimizes paginated list queries (`GET /attendance`) and date-range filtering, enabling MongoDB to satisfy sorting directly from the index tree without in-memory sorting (`SORT` stage).
   - `(emp_code ASC, punch_in DESC)` (Compound): Crucial for `POST /attendance/punch-out`, allowing MongoDB to immediately seek the most recent record whose `punch_in <= punched_at` using an indexed backward range scan.

*Index Plan Verification:* All 5 query paths (`attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`) were inspected via `/admin/explain/{endpoint}` with `executionStats` verbosity. Each path confirmed `IXSCAN` operations and zero `COLLSCAN` stages. Standalone single-field indexing on low-cardinality fields like `status` (5 distinct values) was deliberately rejected to prevent write overhead and index bloat, as compound indexes already provide superior selectivity.

---

### 2. Handling Concurrent Duplicate Punch-Ins (and Punch-Outs)

1. **Punch-In Races:**
   - When multiple requests for the same employee arrive simultaneously for the same attendance date, check-then-insert logic in Python is susceptible to race conditions.
   - We delegate concurrency enforcement to MongoDB's unique compound index on `(emp_code, date)`. Both requests calculate the IST attendance date and execute `insert_one()`.
   - The database atomically serializes the writes: exactly one request succeeds with HTTP 201 Created. The competing request violates the unique constraint, raising a `DuplicateKeyError`, which the application catches and converts to HTTP 409 Conflict.
   - Verified via automated multithreaded testing (`ThreadPoolExecutor` with 10 concurrent requests producing exactly 1 × 201 and 9 × 409s).

2. **Punch-Out Races:**
   - Atomic update semantics are enforced using `find_one_and_update({"_id": record["_id"], "punch_out": None}, ...)`. If two concurrent punch-outs target the same open record, only the first successfully transitions `punch_out` from `None`; the second finds zero matching documents and returns HTTP 409 Conflict.

---

### 3. Leaderboard Ties at the Cutoff

1. **Standard Competition Ranking:**
   - Ranks are computed in MongoDB using `$setWindowFields` with the `$rank` function ordered by `total_late_minutes` descending. This produces standard competition ranks (1, 2, 2, 4) rather than dense ranks or row numbers.
2. **Cutoff Inclusion:**
   - The contract specifies that `limit` applies *after* ranking (`$match: {rank: {"$lte": limit}}`).
   - If multiple employees tie at or below the cutoff rank (for example, two employees tying at rank 10 when `limit=10`), both employees are included in the output. The response can therefore legitimately return more than `limit` rows (e.g., 11 rows).
3. **Deterministic Tie-Breaking:**
   - Tied rows share the identical integer rank. Secondary ordering is applied strictly by `emp_code` ascending (`$sort: {total_late_minutes: -1, _id: 1}`) to ensure predictable, deterministic output across paginated or repeated calls.

---

### 4. Department Headcount for Employees Without Logs

1. **Single Cross-Collection Pipeline:**
   - The department summary calculation is implemented as a single, unified MongoDB aggregation pipeline rooted in the `employees` collection rather than executing multiple queries merged in Python.
2. **Headcount vs. Attendance Separation:**
   - Stage 1 matches `employees` where `joined_on <= last_day_of_month` (and optional department match). This cleanly distinguishes employees who joined by the end of the period from future joiners who joined after the month.
   - Stage 2 performs a `$lookup` into `attendance_logs` matching on `emp_code == $$emp` and `date` within `[first_day, last_day]`.
   - Employees who have zero attendance records in the month produce an empty `logs: []` array.
   - When grouping by department, `headcount` is calculated via `{"$sum": 1}`, accurately counting every employee who joined by month end, regardless of whether they have attendance logs.
   - Metrics such as `present_days`, `late_count`, and `avg_work_hours` aggregate over the joined records via `$reduce`. A department whose headcount is 0 for the month is omitted.

---

### 5. Architectural Changes for 100× Scale (~10 Million Records)

At 100× scale (10,000+ employees, 10M+ attendance records), the following architectural changes would be introduced:

1. **Sharding Strategy:**
   - Shard the `attendance_logs` collection horizontally using a compound shard key: `{ emp_code: "hashed", date: 1 }`. This distributes write traffic uniformly across shards, avoiding write hotspots on a single shard during peak morning punch-in hours, while keeping an individual employee's date records localized for targeted query routing.
2. **Incremental Materialized Rollups:**
   - Rather than scanning millions of raw logs during ad-hoc monthly aggregation queries, maintain pre-aggregated daily and monthly departmental rollups. Using MongoDB Change Streams or CDC (Change Data Capture) pipelines, updates from punch-out and regularizations incrementally update a `department_monthly_stats` collection in real time.
3. **Read/Write Splitting & Replica Routing:**
   - Direct high-throughput OLTP writes (`punch-in`, `punch-out`) to the primary replica set members. Route heavy analytical read requests (`department_trend`, `department_summary`, `late_leaderboard`) to secondary read replicas using `ReadPreference.SECONDARY_PREFERRED`.
4. **Caching Layer:**
   - Deploy a distributed Redis cache for closed historical months (past months whose attendance records are rarely regularized). Historical summaries can be cached with a long TTL, with cache invalidation triggered only by successful `PATCH /attendance/{emp_code}/{date}` regularization events.
