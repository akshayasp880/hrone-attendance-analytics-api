# Employee Attendance & Analytics API

Production-ready backend service built with FastAPI and MongoDB for employee punch logging, regularization audit trails, and attendance analytics pipelines. Fully compliant with the HROne Engineering OpenAPI 3.0.3 specification and business rules (R1–R10).

---

## Tech Stack
* **Language:** Python 3.11+
* **Framework:** FastAPI
* **Database:** MongoDB 6.0+ (Local instance or MongoDB Atlas)
* **Driver:** PyMongo 4.7+

---

## Submission & Project Structure
```text
.
├── app/
│   ├── __init__.py
│   └── main.py              # Complete FastAPI implementation & MongoDB aggregations
├── requirements.txt         # Runtime dependencies (FastAPI, Uvicorn, PyMongo, Pydantic, python-dotenv)
├── REVIEW.md                # Detailed audit & defect analysis of starter code (26 defects documented)
├── DECISIONS.md             # Key architectural decisions (indexing, concurrency, ties, headcount, scale)
├── README.md                # System documentation, seeding guide & testing instructions
├── sample_data/             # Sample datasets for employees and attendance logs
└── sample_seed.py           # Database seeder utility
```

---

## Local Setup & Startup

### 1. Prerequisites
Ensure **Python 3.11+** and **MongoDB 6.0+** are installed and running.
Verify MongoDB service status:
```bash
mongosh --eval "db.adminCommand('ping')"
```

### 2. Environment Setup
Create and activate a clean virtual environment:
```bash
python -m venv .venv

# Linux / macOS:
source .venv/bin/activate

# Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configuration
Configuration parameters are read from environment variables (or an optional local `.env` file):
* `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
* `MONGO_DB`: Target database name (default: `attendance_db`)

```bash
# Optional: Set environment variables
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 5. Start the Server
Run the application with Uvicorn:
```bash
uvicorn app.main:app --port 8000
```
The API is available at `http://localhost:8000`.
* Interactive OpenAPI Docs: `http://localhost:8000/docs`
* Health Readiness Probe: `http://localhost:8000/health` (returns 200 when MongoDB is connected, 503 if unreachable)

---

## Database Seeding

To load the sample data into MongoDB:
```bash
python sample_seed.py
```
This script reads `sample_data/employees.json` and `sample_data/attendance_logs.json` and populates the `employees` and `attendance_logs` collections. All required database indexes are created idempotently at application startup.

---

## Testing & Verification Guide

### 1. Running the Automated Test Suites
To execute the automated unit and integration tests, install test dependencies (`pytest`, `httpx`):
```bash
pip install pytest httpx
pytest -v
```

To run the audit-specific test suite verifying all P0/P1 fixes and multithreaded concurrency:
```bash
pytest -v test_audit_findings.py
```

### 2. Manual Verification of Core Business Rules

#### Health Check
```bash
curl -X GET http://localhost:8000/health
# Response: {"status":"ok"}
```

#### Create Employee
```bash
curl -X POST http://localhost:8000/employees \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP0100",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-01"
  }'
```

#### Punch-In (R1 & R2)
```bash
# 2026-07-06 04:10:01 UTC = 09:40:01 IST (10 min late per R2)
curl -X POST http://localhost:8000/attendance/punch-in \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP0100",
    "punched_at": 1783311001000,
    "status": "PRESENT"
  }'
```

#### Punch-Out (R3, R4, R5)
```bash
# 2026-07-06 13:40:00 UTC = 19:10:00 IST (40 min OT per R3, 9.50 work hours)
curl -X POST http://localhost:8000/attendance/punch-out \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP0100",
    "punched_at": 1783345200000
  }'
```

#### Department Summary (Single Cross-Collection Aggregation)
```bash
curl -X GET "http://localhost:8000/analytics/departments/summary?month=2026-07"
```

#### Department Trend (Gap-Filling & Window Moving Average)
```bash
curl -X GET "http://localhost:8000/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10"
```

#### Admin Explain Plans (`IXSCAN` verification)
```bash
curl -X GET "http://localhost:8000/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10"
curl -X GET "http://localhost:8000/admin/explain/department_summary?month=2026-07"
```

---

## Architectural Highlights
1. **Concurrency Safety:**
   - **Punch-In:** Atomic deduplication via MongoDB compound unique index `("emp_code", "date")`, translating concurrent duplicate attempts to HTTP 409.
   - **Punch-Out:** Record selection queries the most recent record where `punch_in <= punched_at`, updated atomically via `find_one_and_update({"_id": record["_id"], "punch_out": None}, ...)`.
   - **Regularization:** Optimistic concurrency control via `{"history": {"$size": hist_len}}`, rejecting conflicting concurrent edits with HTTP 409.
2. **Analytics & Rounding:**
   - Pure MongoDB aggregation pipelines for all analytical endpoints without Python processing loops.
   - Decimal128 mathematical expressions implementing true IEEE half-up rounding (`ROUND_HALF_UP`) to 4 decimal places for rates and rolling averages, avoiding MongoDB's default ties-to-even `$round`.
   - Continuous date generation via MongoDB `$densify` with explicit range bounds, computing rolling 7-day moving averages (`[-6, "current"]`) across weekends and missing dates.
3. **Index Optimization:**
   - All query paths validated with `executionStats` confirming `IXSCAN` operations and 0 collection scans (`COLLSCAN`).
