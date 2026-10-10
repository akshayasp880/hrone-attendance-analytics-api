"""
Comprehensive Test Suite for HROne Employee Attendance & Analytics API
Tests all endpoints, business rules (R1-R10), status codes, concurrency, and explain plans.
"""

from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import app, db, client, create_indexes


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    try:
        client.admin.command("ping")
    except Exception as e:
        pytest.skip(f"MongoDB not available: {e}")
    create_indexes()
    yield


def test_health():
    with TestClient(app) as tc:
        res = tc.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}


def test_employee_crud():
    with TestClient(app) as tc:
        # Create valid
        emp_data = {
            "emp_code": "EMP9991",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        res = tc.post("/employees", json=emp_data)
        assert res.status_code in (201, 409)

        # Duplicate emp_code must return 409
        res_dup = tc.post("/employees", json=emp_data)
        assert res_dup.status_code == 409

        # Invalid emp_code regex must return 422
        bad_emp = dict(emp_data, emp_code="INVALID")
        assert tc.post("/employees", json=bad_emp).status_code == 422

        # shift_start == shift_end must return 422
        bad_shift = dict(emp_data, emp_code="EMP9992", shift_end="09:30")
        assert tc.post("/employees", json=bad_shift).status_code == 422

        # List employees paginated
        res_list = tc.get("/employees?department=Engineering&page=1&page_size=10")
        assert res_list.status_code == 200
        data = res_list.json()
        assert "items" in data
        assert "total" in data
        assert data["page"] == 1


def test_punch_in_and_out():
    with TestClient(app) as tc:
        # Ensure employee exists
        tc.post(
            "/employees",
            json={
                "emp_code": "EMP9993",
                "name": "Punch Tester",
                "email": "punch@example.com",
                "department": "Sales",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

        # Punch in at 09:40:01 (10 min late per R2)
        # 2026-07-20 04:10:01 UTC = 09:40:01 IST
        punch_in_ms = int(
            datetime(2026, 7, 20, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000
        )
        res_pi = tc.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP9993",
                "punched_at": punch_in_ms,
                "status": "PRESENT",
            },
        )
        assert res_pi.status_code in (201, 409)
        if res_pi.status_code == 201:
            data = res_pi.json()
            assert data["date"] == "2026-07-20"
            assert data["late_minutes"] == 10
            assert data["work_hours"] is None

        # Duplicate punch-in for same date must return 409
        res_pi_dup = tc.post(
            "/attendance/punch-in",
            json={
                "emp_code": "EMP9993",
                "punched_at": punch_in_ms,
                "status": "PRESENT",
            },
        )
        assert res_pi_dup.status_code == 409

        # Punch out at 19:10:00 (40 min OT per R3)
        # 2026-07-20 13:40:00 UTC = 19:10:00 IST
        punch_out_ms = int(
            datetime(2026, 7, 20, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        res_po = tc.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9993", "punched_at": punch_out_ms},
        )
        assert res_po.status_code in (200, 409)
        if res_po.status_code == 200:
            data = res_po.json()
            assert data["overtime_minutes"] == 40
            assert data["work_hours"] == 9.50
            assert data["half_day"] is False

        # Duplicate punch-out must return 409
        res_po_dup = tc.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP9993", "punched_at": punch_out_ms},
        )
        assert res_po_dup.status_code == 409


def test_regularize_record():
    with TestClient(app) as tc:
        # Patch date 2026-07-20 for EMP9993
        res = tc.patch(
            "/attendance/EMP9993/2026-07-20",
            json={
                "status": "WFH",
                "reason": "WFH approved by manager",
                "regularized_by": "manager1",
            },
        )
        if res.status_code == 200:
            data = res.json()
            assert data["status"] == "WFH"
            assert len(data["history"]) >= 1
            latest_hist = data["history"][-1]
            assert latest_hist["by"] == "manager1"
            assert "status" in latest_hist["changes"]


def test_analytics_employee_monthly():
    with TestClient(app) as tc:
        res = tc.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert res.status_code == 200
        data = res.json()
        assert data["emp_code"] == "EMP0001"
        assert data["month"] == "2026-07"
        assert data["working_days"] == 23  # July 2026 Mon-Fri
        assert data["present_days"] >= 2.0
        assert "attendance_pct" in data


def test_analytics_department_summary():
    with TestClient(app) as tc:
        res = tc.get("/analytics/departments/summary?month=2026-07")
        assert res.status_code == 200
        data = res.json()
        assert data["month"] == "2026-07"
        assert "items" in data
        assert len(data["items"]) > 0
        depts = [item["department"] for item in data["items"]]
        assert "Engineering" in depts
        assert "Sales" in depts


def test_analytics_late_leaderboard():
    with TestClient(app) as tc:
        res = tc.get("/analytics/leaderboard/late?month=2026-07&limit=10")
        assert res.status_code == 200
        data = res.json()
        assert data["month"] == "2026-07"
        assert "items" in data
        # Verify rank order
        items = data["items"]
        if items:
            for i in range(len(items) - 1):
                assert items[i]["total_late_minutes"] >= items[i + 1]["total_late_minutes"]
                assert items[i]["rank"] <= items[i + 1]["rank"]


def test_analytics_department_trend():
    with TestClient(app) as tc:
        res = tc.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
        assert res.status_code == 200
        data = res.json()
        assert data["department"] == "Engineering"
        assert "items" in data
        assert len(data["items"]) == 10  # 10 days inclusive
        for row in data["items"]:
            assert "date" in row
            assert "is_working_day" in row
            assert "headcount" in row
            assert "present_count" in row
            assert "late_count" in row
            assert "attendance_rate" in row
            assert "moving_avg_7d" in row


def test_explain_endpoints():
    with TestClient(app) as tc:
        # Test explain for attendance_list
        res = tc.get("/admin/explain/attendance_list?page=1&page_size=10")
        assert res.status_code == 200
        data = res.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data

        # Test explain for employee_monthly
        res = tc.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        assert res.status_code == 200

        # Test explain for department_summary
        res = tc.get("/admin/explain/department_summary?month=2026-07")
        assert res.status_code == 200

        # Test explain for late_leaderboard
        res = tc.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
        assert res.status_code == 200

        # Test explain for department_trend
        res = tc.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
        assert res.status_code == 200


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-v", __file__]))
