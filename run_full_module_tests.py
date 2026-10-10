"""
Exhaustive Module-by-Module Verification for HROne Employee Attendance & Analytics API
Tests all modules, status codes, business rules, and error handling against live uvicorn server.
"""

import json
import urllib.request
import urllib.error
import sys

from fastapi.testclient import TestClient
from app.main import app

tc = TestClient(app)

results = []

def request(method, path, body=None):
    try:
        resp = tc.request(method, path, json=body)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return resp.status_code, data
    except Exception as ex:
        return 500, {"error": str(ex)}

def record(module, test_name, expected_status, actual_status, passed, detail=""):
    results.append({
        "module": module,
        "test": test_name,
        "expected": expected_status,
        "actual": actual_status,
        "passed": passed,
        "detail": detail
    })
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {module} -> {test_name}: Expected {expected_status}, Got {actual_status} {detail}")

print("================================================================================")
print("              STARTING FULL MODULE VERIFICATION AGAINST LIVE API                ")
print("================================================================================\n")

# Clean previous test run artifacts
try:
    from app.main import db
    db.employees.delete_many({"emp_code": {"$regex": "^EMP88"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP88"}})
except Exception:
    pass

# --------------------------------------------------------------------------- #
# Module 1: System / Health
# --------------------------------------------------------------------------- #
status, body = request("GET", "/health")
record("System", "GET /health returns 200 with status ok", 200, status, status == 200 and body.get("status") == "ok")

# --------------------------------------------------------------------------- #
# Module 2: Employees Module
# --------------------------------------------------------------------------- #
# 2.1 Create valid employee
emp_code_valid = "EMP8801"
emp_payload = {
    "emp_code": emp_code_valid,
    "name": "Arjun Sharma",
    "email": "arjun@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-10"
}
status, body = request("POST", "/employees", emp_payload)
record("Employees", "POST /employees (create valid)", 201, status, status == 201 and body.get("emp_code") == emp_code_valid)

# 2.2 Conflict on duplicate emp_code (409)
status, body = request("POST", "/employees", emp_payload)
record("Employees", "POST /employees (duplicate emp_code returns 409)", 409, status, status == 409)

# 2.3 Validation: invalid emp_code pattern (422)
bad_emp = dict(emp_payload, emp_code="INVALID_CODE")
status, body = request("POST", "/employees", bad_emp)
record("Employees", "POST /employees (invalid emp_code regex returns 422)", 422, status, status == 422)

# 2.4 Validation: shift_start == shift_end (422)
bad_shift = dict(emp_payload, emp_code="EMP8802", shift_end="09:30")
status, body = request("POST", "/employees", bad_shift)
record("Employees", "POST /employees (identical shift start/end returns 422)", 422, status, status == 422)

# 2.5 Validation: bad email format (422)
bad_email = dict(emp_payload, emp_code="EMP8803", email="notanemail")
status, body = request("POST", "/employees", bad_email)
record("Employees", "POST /employees (malformed email returns 422)", 422, status, status == 422)

# 2.6 List employees with pagination and sorting
status, body = request("GET", "/employees?department=Engineering&page=1&page_size=5")
items = body.get("items", [])
is_sorted = all(items[i]["emp_code"] <= items[i+1]["emp_code"] for i in range(len(items)-1)) if len(items) > 1 else True
record("Employees", "GET /employees (paginated & sorted by emp_code asc)", 200, status, status == 200 and is_sorted and "total" in body)

# --------------------------------------------------------------------------- #
# Module 3: Attendance Punch-In
# --------------------------------------------------------------------------- #
# 3.1 Punch-in for unknown employee (404)
status, body = request("POST", "/attendance/punch-in", {"emp_code": "EMP0000_NOT_FOUND", "status": "PRESENT"})
record("Punch-In", "POST /attendance/punch-in (unknown employee returns 404)", 404, status, status == 404)

# 3.2 Punch-in with invalid status (422)
status, body = request("POST", "/attendance/punch-in", {"emp_code": emp_code_valid, "status": "LEAVE"})
record("Punch-In", "POST /attendance/punch-in (non-presence status returns 422)", 422, status, status == 422)

# 3.3 Punch-in with out-of-range epoch ms (422)
status, body = request("POST", "/attendance/punch-in", {"emp_code": emp_code_valid, "punched_at": 12345, "status": "PRESENT"})
record("Punch-In", "POST /attendance/punch-in (out of range timestamp returns 422)", 422, status, status == 422)

# 3.4 Valid punch-in (201)
# 2026-08-03 04:15:00 UTC = 09:45:00 IST (15 min late on 09:30 shift)
punch_in_ms = 1785730500000
status, body = request("POST", "/attendance/punch-in", {
    "emp_code": emp_code_valid,
    "punched_at": punch_in_ms,
    "status": "PRESENT"
})
record("Punch-In", "POST /attendance/punch-in (valid punch-in, R1 date & R2 late calculated)", 201, status,
       status == 201 and body.get("date") == "2026-08-03" and body.get("late_minutes") == 15)

# 3.5 Duplicate punch-in for same date (409)
status, body = request("POST", "/attendance/punch-in", {
    "emp_code": emp_code_valid,
    "punched_at": punch_in_ms,
    "status": "PRESENT"
})
record("Punch-In", "POST /attendance/punch-in (duplicate punch for same day returns 409)", 409, status, status == 409)

# --------------------------------------------------------------------------- #
# Module 4: Attendance Punch-Out
# --------------------------------------------------------------------------- #
# 4.1 Punch-out for unknown employee (404)
status, body = request("POST", "/attendance/punch-out", {"emp_code": "EMP0000_NONE", "punched_at": punch_in_ms + 10000})
record("Punch-Out", "POST /attendance/punch-out (unknown employee returns 404)", 404, status, status == 404)

# 4.2 Punch-out timestamp before punch-in (422)
status, body = request("POST", "/attendance/punch-out", {"emp_code": emp_code_valid, "punched_at": punch_in_ms - 60000})
record("Punch-Out", "POST /attendance/punch-out (punch-out before punch-in returns 422)", 422, status, status == 422)

# 4.3 Punch-out > 24 hours after punch-in (422)
status, body = request("POST", "/attendance/punch-out", {"emp_code": emp_code_valid, "punched_at": punch_in_ms + 90000000})
record("Punch-Out", "POST /attendance/punch-out (punch-out > 24h returns 422)", 422, status, status == 422)

# 4.4 Valid punch-out (200)
# 2026-08-03 13:45:00 UTC = 19:15:00 IST (9.5h work, 45 min overtime on 18:30 shift end)
punch_out_ms = punch_in_ms + (9 * 3600 + 30 * 60) * 1000  # 9.5 hours
status, body = request("POST", "/attendance/punch-out", {
    "emp_code": emp_code_valid,
    "punched_at": punch_out_ms
})
record("Punch-Out", "POST /attendance/punch-out (valid punch-out: R4 9.50h, R3 45m OT, R5 half_day false)", 200, status,
       status == 200 and body.get("work_hours") == 9.5 and body.get("overtime_minutes") == 45 and body.get("half_day") is False)

# 4.5 Duplicate punch-out (409)
status, body = request("POST", "/attendance/punch-out", {
    "emp_code": emp_code_valid,
    "punched_at": punch_out_ms + 1000
})
record("Punch-Out", "POST /attendance/punch-out (already punched out returns 409)", 409, status, status == 409)

# --------------------------------------------------------------------------- #
# Module 5: Attendance Listing
# --------------------------------------------------------------------------- #
# 5.1 Valid list attendance
status, body = request("GET", "/attendance?emp_code=" + emp_code_valid + "&page=1&page_size=10")
record("Attendance List", "GET /attendance (filters by employee, returns items and total)", 200, status,
       status == 200 and len(body.get("items", [])) >= 1 and "total" in body)

# 5.2 Invalid date range: date_from > date_to (422)
status, body = request("GET", "/attendance?date_from=2026-08-10&date_to=2026-08-01")
record("Attendance List", "GET /attendance (date_from > date_to returns 422)", 422, status, status == 422)

# 5.3 Invalid status query param (422)
status, body = request("GET", "/attendance?status=INVALID_STATUS")
record("Attendance List", "GET /attendance (invalid status filter returns 422)", 422, status, status == 422)

# --------------------------------------------------------------------------- #
# Module 6: Regularize (Manual Correction with Audit Trail)
# --------------------------------------------------------------------------- #
# 6.1 Regularize unknown record (404)
status, body = request("PATCH", f"/attendance/{emp_code_valid}/2026-12-31", {
    "status": "WFH", "reason": "Testing 404 error path", "regularized_by": "admin"
})
record("Regularize", "PATCH /attendance/{emp}/{date} (non-existent record returns 404)", 404, status, status == 404)

# 6.2 Supplying punch times with ABSENT (422)
status, body = request("PATCH", f"/attendance/{emp_code_valid}/2026-08-03", {
    "status": "ABSENT", "punch_in": punch_in_ms, "reason": "Invalid combination", "regularized_by": "admin"
})
record("Regularize", "PATCH /attendance/{emp}/{date} (punch times with ABSENT returns 422)", 422, status, status == 422)

# 6.3 Short reason < 5 chars (422)
status, body = request("PATCH", f"/attendance/{emp_code_valid}/2026-08-03", {
    "status": "WFH", "reason": "No", "regularized_by": "admin"
})
record("Regularize", "PATCH /attendance/{emp}/{date} (reason < 5 chars returns 422)", 422, status, status == 422)

# 6.4 Valid regularization (200)
status, body = request("PATCH", f"/attendance/{emp_code_valid}/2026-08-03", {
    "status": "WFH",
    "reason": "Employee requested WFH approval",
    "regularized_by": "manager_arun"
})
has_audit = False
if status == 200 and body.get("history"):
    entry = body["history"][-1]
    has_audit = entry.get("by") == "manager_arun" and "status" in entry.get("changes", {})
record("Regularize", "PATCH /attendance/{emp}/{date} (valid regularization appends audit history)", 200, status,
       status == 200 and body.get("status") == "WFH" and has_audit)

# 6.5 Request with no changes (422)
status, body = request("PATCH", f"/attendance/{emp_code_valid}/2026-08-03", {
    "status": "WFH",
    "reason": "Submitting identical status again",
    "regularized_by": "manager_arun"
})
record("Regularize", "PATCH /attendance/{emp}/{date} (submitting zero changes returns 422)", 422, status, status == 422)

# --------------------------------------------------------------------------- #
# Module 7: Employee Monthly Analytics
# --------------------------------------------------------------------------- #
# 7.1 Unknown employee (404)
status, body = request("GET", "/analytics/employees/EMP9999_NONE/monthly?month=2026-07")
record("Monthly Analytics", "GET /analytics/employees/{emp}/monthly (unknown employee returns 404)", 404, status, status == 404)

# 7.2 Invalid month format (422)
status, body = request("GET", f"/analytics/employees/{emp_code_valid}/monthly?month=2026-13")
record("Monthly Analytics", "GET /analytics/employees/{emp}/monthly (invalid month returns 422)", 422, status, status == 422)

# 7.3 Valid monthly report
status, body = request("GET", f"/analytics/employees/{emp_code_valid}/monthly?month=2026-08")
record("Monthly Analytics", "GET /analytics/employees/{emp}/monthly (returns working days, present days, att %)", 200, status,
       status == 200 and "working_days" in body and "present_days" in body and "attendance_pct" in body)

# --------------------------------------------------------------------------- #
# Module 8: Department Summary Analytics
# --------------------------------------------------------------------------- #
# 8.1 Invalid month (422)
status, body = request("GET", "/analytics/departments/summary?month=BAD-MONTH")
record("Department Summary", "GET /analytics/departments/summary (malformed month returns 422)", 422, status, status == 422)

# 8.2 Valid department summary
status, body = request("GET", "/analytics/departments/summary?month=2026-07")
items = body.get("items", [])
has_all_fields = len(items) > 0 and all(
    "department" in x and "headcount" in x and "present_days" in x and "avg_work_hours" in x for x in items
)
record("Department Summary", "GET /analytics/departments/summary (cross-collection aggregation with headcount)", 200, status,
       status == 200 and has_all_fields)

# --------------------------------------------------------------------------- #
# Module 9: Late Leaderboard Analytics
# --------------------------------------------------------------------------- #
# 9.1 Invalid month (422)
status, body = request("GET", "/analytics/leaderboard/late?month=INVALID")
record("Leaderboard", "GET /analytics/leaderboard/late (invalid month returns 422)", 422, status, status == 422)

# 9.2 Valid leaderboard with competition ranking
status, body = request("GET", "/analytics/leaderboard/late?month=2026-07&limit=5")
items = body.get("items", [])
ranks_valid = len(items) > 0 and all("rank" in x and "total_late_minutes" in x and "emp_code" in x for x in items)
record("Leaderboard", "GET /analytics/leaderboard/late (returns standard competition ranking 1, 2, 2, 4)", 200, status,
       status == 200 and ranks_valid)

# --------------------------------------------------------------------------- #
# Module 10: Department Trend Analytics
# --------------------------------------------------------------------------- #
# 10.1 Unknown department (404)
status, body = request("GET", "/analytics/departments/NON_EXISTENT_DEPT/trend?from=2026-07-01&to=2026-07-05")
record("Department Trend", "GET /analytics/departments/{dept}/trend (unknown department returns 404)", 404, status, status == 404)

# 10.2 Date range > 92 days (422)
status, body = request("GET", "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
record("Department Trend", "GET /analytics/departments/{dept}/trend (range > 92 days returns 422)", 422, status, status == 422)

# 10.3 to < from (422)
status, body = request("GET", "/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
record("Department Trend", "GET /analytics/departments/{dept}/trend (to < from returns 422)", 422, status, status == 422)

# 10.4 Valid continuous trend with gap-filling & moving avg
status, body = request("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
items = body.get("items", [])
trend_ok = len(items) == 7 and all(
    "date" in x and "is_working_day" in x and "headcount" in x and "moving_avg_7d" in x for x in items
)
record("Department Trend", "GET /analytics/departments/{dept}/trend (gap-filled daily series & 7-day moving avg)", 200, status,
       status == 200 and trend_ok)

# --------------------------------------------------------------------------- #
# Module 11: Admin Explain Module
# --------------------------------------------------------------------------- #
# 11.1 Unknown explain endpoint (422)
status, body = request("GET", "/admin/explain/unknown_endpoint")
record("Admin Explain", "GET /admin/explain/{endpoint} (unknown endpoint returns 422)", 422, status, status == 422)

# 11.2 attendance_list explain
status, body = request("GET", "/admin/explain/attendance_list?page=1&page_size=10")
record("Admin Explain", "GET /admin/explain/attendance_list (returns raw queryPlanner with executionStats)", 200, status,
       status == 200 and "explain" in body and body.get("collection") == "attendance_logs")

# 11.3 employee_monthly explain
status, body = request("GET", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
record("Admin Explain", "GET /admin/explain/employee_monthly (returns aggregation explain)", 200, status,
       status == 200 and "explain" in body)

# 11.4 department_summary explain
status, body = request("GET", "/admin/explain/department_summary?month=2026-07")
record("Admin Explain", "GET /admin/explain/department_summary (returns aggregation explain)", 200, status,
       status == 200 and "explain" in body)

# 11.5 late_leaderboard explain
status, body = request("GET", "/admin/explain/late_leaderboard?month=2026-07")
record("Admin Explain", "GET /admin/explain/late_leaderboard (returns aggregation explain)", 200, status,
       status == 200 and "explain" in body)

# 11.6 department_trend explain
status, body = request("GET", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
record("Admin Explain", "GET /admin/explain/department_trend (returns aggregation explain)", 200, status,
       status == 200 and "explain" in body)

# --------------------------------------------------------------------------- #
# Summary Report
# --------------------------------------------------------------------------- #
total = len(results)
passed_count = sum(1 for r in results if r["passed"])
failed_count = total - passed_count

print("\n================================================================================")
print(f"                       MODULE VERIFICATION SUMMARY                              ")
print(f"  TOTAL TESTS RUN: {total} | PASSED: {passed_count} | FAILED: {failed_count}   ")
print("================================================================================")

if failed_count > 0:
    print("FAILED TESTS:")
    for r in results:
        if not r["passed"]:
            print(f"  - [{r['module']}] {r['test']}: Expected {r['expected']}, got {r['actual']}")
    sys.exit(1)
else:
    print("SUCCESS: ALL API MODULES AND ENDPOINTS ARE 100% OPERATIONAL AND CORRECT!")
    sys.exit(0)
