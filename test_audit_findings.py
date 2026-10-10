import pytest
from datetime import datetime, timezone, date, timedelta
from fastapi.testclient import TestClient
from app.main import app, db, client, create_indexes

@pytest.fixture(scope="session", autouse=True)
def setup_db():
    create_indexes()
    yield

def test_audit_p0_punch_out_record_selection():
    """Verify that punch-out selects the record with punch_in <= punched_at, not newer records."""
    with TestClient(app) as tc:
        emp_code = "EMP8801"
        db.employees.delete_one({"emp_code": emp_code})
        db.attendance_logs.delete_many({"emp_code": emp_code})

        # Create employee
        tc.post("/employees", json={
            "emp_code": emp_code,
            "name": "Audit Tester 1",
            "email": "audit1@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        # Day 1: 2026-07-06 09:30 IST = 04:00 UTC
        d1_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        # Day 2: 2026-07-07 09:30 IST = 04:00 UTC
        d2_in = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

        # Punch in Day 1
        r = tc.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": d1_in})
        assert r.status_code == 201

        # Punch in Day 2
        r = tc.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": d2_in})
        assert r.status_code == 201

        # Now send punch-out for Day 1: 2026-07-06 18:30 IST = 13:00 UTC
        # This is earlier than Day 2 punch-in!
        d1_out = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_po = tc.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": d1_out})
        assert r_po.status_code == 200
        po_data = r_po.json()
        assert po_data["date"] == "2026-07-06", "Must close Day 1 record, not Day 2!"
        assert po_data["work_hours"] == 9.0

        # Attempt to punch out Day 1 again -> must return 409
        r_po_dup = tc.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": d1_out})
        assert r_po_dup.status_code == 409

        # Punch out with timestamp earlier than any punch-in -> must return 422 (punched_at <= punch_in)
        too_early = int(datetime(2026, 7, 5, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_po_early = tc.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": too_early})
        assert r_po_early.status_code == 422

        # Punch out when employee has zero punch-ins at all -> must return 404 (no punch-in found)
        db.attendance_logs.delete_many({"emp_code": emp_code})
        r_po_none = tc.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": d1_out})
        assert r_po_none.status_code == 404

        # Clean up
        db.employees.delete_one({"emp_code": emp_code})
        db.attendance_logs.delete_many({"emp_code": emp_code})

def test_audit_p0_trend_zero_data_fallback_and_window():
    """Verify department trend when a department has zero attendance records."""
    with TestClient(app) as tc:
        dept = "ZeroDept"
        emp_code = "EMP8802"
        db.employees.delete_many({"department": dept})
        db.attendance_logs.delete_many({"emp_code": emp_code})

        # Create employee in ZeroDept joined before range
        tc.post("/employees", json={
            "emp_code": emp_code,
            "name": "Zero Tester",
            "email": "zero@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-06-01"
        })

        # Query trend for 2026-07-01 (Wed) to 2026-07-05 (Sun) -> 5 days
        r = tc.get(f"/analytics/departments/{dept}/trend?from=2026-07-01&to=2026-07-05")
        assert r.status_code == 200
        data = r.json()
        items = data["items"]
        assert len(items) == 5

        # Check Wed (07-01), Thu (07-02), Fri (07-03)
        for i in range(3):
            assert items[i]["is_working_day"] is True
            assert items[i]["headcount"] == 1
            assert items[i]["present_count"] == 0
            assert items[i]["attendance_rate"] == 0.0
            assert items[i]["moving_avg_7d"] == 0.0

        # Check Sat (07-04), Sun (07-05): weekend
        for i in (3, 4):
            assert items[i]["is_working_day"] is False
            assert items[i]["attendance_rate"] is None
            # Rolling average must NOT be None because preceding days had rate 0.0!
            assert items[i]["moving_avg_7d"] == 0.0

        # Clean up
        db.employees.delete_many({"department": dept})

def test_audit_p0_explain_validation_parity():
    """Verify all explain branches validate inputs like the real endpoints."""
    with TestClient(app) as tc:
        # 1. Invalid month pattern / calendar in employee_monthly explain
        r = tc.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13")
        assert r.status_code == 422

        r = tc.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=invalid")
        assert r.status_code == 422

        # 2. Missing emp_code in employee_monthly explain
        r = tc.get("/admin/explain/employee_monthly?month=2026-07")
        assert r.status_code == 422

        # 3. Invalid status in attendance_list explain
        r = tc.get("/admin/explain/attendance_list?status=NOT_A_STATUS")
        assert r.status_code == 422

        # 4. Reversed date range in attendance_list explain
        r = tc.get("/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-01")
        assert r.status_code == 422

        # 5. Invalid date format in attendance_list explain
        r = tc.get("/admin/explain/attendance_list?date_from=2026-02-30")
        assert r.status_code == 422

        # 6. Trend explain: to < from
        r = tc.get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-01")
        assert r.status_code == 422

        # 7. Trend explain: range > 92 days
        r = tc.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-05")
        assert r.status_code == 422

        # 8. Trend explain: invalid date format
        r = tc.get("/admin/explain/department_trend?department=Engineering&from=2026-02-30&to=2026-03-01")
        assert r.status_code == 422

def test_audit_p1_trend_92_days_inclusive_boundary():
    """Verify trend accepts exactly 92 calendar days inclusive and rejects 93 days."""
    with TestClient(app) as tc:
        # 2026-01-01 to 2026-04-02: Jan (31) + Feb (28) + Mar (31) + Apr (2) = 92 days
        r92 = tc.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-02")
        assert r92.status_code == 200
        assert len(r92.json()["items"]) == 92

        # 2026-01-01 to 2026-04-03: 93 days -> must return 422
        r93 = tc.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-03")
        assert r93.status_code == 422

def test_audit_p1_attendance_date_validation():
    """Verify list_attendance validates date formats and invalid calendar dates."""
    with TestClient(app) as tc:
        # Invalid day 30 in February
        r = tc.get("/attendance?date_from=2026-02-30")
        assert r.status_code == 422

        # Invalid month 13
        r = tc.get("/attendance?date_to=2026-13-01")
        assert r.status_code == 422

        # Garbage string
        r = tc.get("/attendance?date_from=not-a-date")
        assert r.status_code == 422

        # Reversed dates
        r = tc.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
        assert r.status_code == 422

def test_audit_p1_department_summary_cross_collection():
    """Verify department summary handles employees with 0 logs and future joiners correctly."""
    with TestClient(app) as tc:
        dept = "AuditDept"
        db.employees.delete_many({"department": dept})
        db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP880"}})

        # Employee 1: joined 2026-07-01, has logs
        tc.post("/employees", json={
            "emp_code": "EMP8803",
            "name": "Dept Emp 1",
            "email": "emp1@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
        # Employee 2: joined 2026-07-15, ZERO logs
        tc.post("/employees", json={
            "emp_code": "EMP8804",
            "name": "Dept Emp 2",
            "email": "emp2@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-15"
        })
        # Employee 3: joined 2026-08-01 (FUTURE joiner, must be excluded from July summary)
        tc.post("/employees", json={
            "emp_code": "EMP8805",
            "name": "Dept Emp 3",
            "email": "emp3@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01"
        })

        # Punch in for Emp 1 on 2026-07-06 (Mon)
        p_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        p_out = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        tc.post("/attendance/punch-in", json={"emp_code": "EMP8803", "punched_at": p_in})
        tc.post("/attendance/punch-out", json={"emp_code": "EMP8803", "punched_at": p_out})

        res = tc.get(f"/analytics/departments/summary?month=2026-07&department={dept}")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        d_item = items[0]
        # Headcount must be 2 (EMP8803 and EMP8804, EMP8805 excluded)
        assert d_item["headcount"] == 2
        assert d_item["present_days"] == 1.0
        assert d_item["avg_work_hours"] == 9.0

        # Clean up
        db.employees.delete_many({"department": dept})
        db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP880"}})

def test_concurrency_duplicate_punch_in():
    """Verify simultaneous duplicate punch-ins yield exactly one 201 and rest 409."""
    import concurrent.futures
    emp_code = "EMP8899"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    with TestClient(app) as tc:
        tc.post("/employees", json={
            "emp_code": emp_code,
            "name": "Race Tester",
            "email": "race@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        ts = int(datetime(2026, 7, 15, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

        def do_punch():
            with TestClient(app) as local_tc:
                return local_tc.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts}).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(do_punch) for _ in range(10)]
            statuses = [f.result() for f in futures]

        assert statuses.count(201) == 1, f"Expected exactly one 201, got: {statuses}"
        assert statuses.count(409) == 9, f"Expected 9 409s, got: {statuses}"

        db.employees.delete_one({"emp_code": emp_code})
        db.attendance_logs.delete_many({"emp_code": emp_code})

def test_concurrency_duplicate_punch_out():
    """Verify simultaneous duplicate punch-outs yield exactly one 200 and rest 409."""
    import concurrent.futures
    emp_code = "EMP8898"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    with TestClient(app) as tc:
        tc.post("/employees", json={
            "emp_code": emp_code,
            "name": "Race Tester 2",
            "email": "race2@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        ts_in = int(datetime(2026, 7, 15, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        ts_out = int(datetime(2026, 7, 15, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

        r_in = tc.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts_in})
        assert r_in.status_code == 201

        def do_punch_out():
            with TestClient(app) as local_tc:
                return local_tc.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ts_out}).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(do_punch_out) for _ in range(10)]
            statuses = [f.result() for f in futures]

        assert statuses.count(200) == 1, f"Expected exactly one 200, got: {statuses}"
        assert statuses.count(409) == 9, f"Expected 9 409s, got: {statuses}"

        db.employees.delete_one({"emp_code": emp_code})
        db.attendance_logs.delete_many({"emp_code": emp_code})

if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-v", __file__]))
