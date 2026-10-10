"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
def create_indexes():
    # employees collection
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")

    # attendance_logs collection
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value, decimals=2):
    """Round using ROUND_HALF_UP in Python (R8)."""
    if value is None:
        return None
    d = Decimal(str(value))
    places = Decimal(10) ** -decimals
    return float(d.quantize(places, rounding=ROUND_HALF_UP))


def sanitize_bson(obj):
    """Recursively convert BSON-specific types (like Decimal128) into standard JSON types."""
    if isinstance(obj, Decimal128):
        return float(str(obj))
    if isinstance(obj, dict):
        return {k: sanitize_bson(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_bson(v) for v in obj]
    return obj


def mongo_round_half_up_expr(field_expr, decimals=4):
    """
    MongoDB aggregation expression implementing IEEE ROUND_HALF_UP (R8).
    Uses Decimal128 arithmetic: floor(x * 10^decimals + 0.5) / 10^decimals.
    """
    mult = Decimal128(str(10**decimals))
    half = Decimal128("0.5")
    return {
        "$toDouble": {
            "$divide": [
                {
                    "$floor": {
                        "$add": [
                            {"$multiply": [{"$toDecimal": field_expr}, mult]},
                            half,
                        ]
                    }
                },
                mult,
            ]
        }
    }


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime, truncated to whole seconds."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def dt_to_epoch_ms(dt_val) -> Optional[int]:
    """Convert a datetime to epoch milliseconds, or None if None."""
    if dt_val is None:
        return None
    return int(dt_val.timestamp() * 1000)


def validate_epoch_ms(value) -> int:
    """Validate and return an epoch-milliseconds integer, or raise 422."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, detail=[{"msg": "must be an integer (epoch ms)"}])
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, detail=[{"msg": "epoch ms out of range"}])
    return value


def validate_date_str(d_str: Optional[str]) -> Optional[date]:
    """Validate and parse a YYYY-MM-DD string into a date, or raise 422."""
    if d_str is None:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d_str):
        raise HTTPException(
            422, detail=[{"msg": "invalid date format, expected YYYY-MM-DD"}]
        )
    try:
        return date.fromisoformat(d_str)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": "invalid calendar date"}])


def parse_month(month_str: str) -> tuple[date, date]:
    """Validate and return (first_day, last_day) for 'YYYY-MM', or raise 422."""
    if not month_str or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month_str):
        raise HTTPException(
            422, detail=[{"msg": "invalid month format, expected YYYY-MM"}]
        )
    year, mon = map(int, month_str.split("-"))
    first = date(year, mon, 1)
    if mon == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, mon + 1, 1) - timedelta(days=1)
    return first, last


def get_attendance_date(
    punch_in_utc: datetime, shift_start: str, shift_end: str
) -> str:
    """
    Determine the attendance date (IST calendar date) from a punch-in instant.
    For overnight shifts (shift_end <= shift_start): if the IST time of the
    punch-in is earlier than shift_end, the attendance date is the previous
    calendar day (R1).
    """
    punch_ist = punch_in_utc.astimezone(IST)
    att_date = punch_ist.date()

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    start_min = sh * 60 + sm
    end_min = eh * 60 + em

    if end_min <= start_min:  # overnight shift
        punch_time = time(punch_ist.hour, punch_ist.minute, punch_ist.second)
        shift_end_time = time(eh, em, 0)
        if punch_time < shift_end_time:
            att_date -= timedelta(days=1)

    return att_date.isoformat()


def compute_late(punch_in_utc: datetime, shift_start: str, att_date_str: str) -> int:
    """
    R2: Grace = 10 min. Late only if strictly more than 10:00 after shift_start.
    late_minutes = floor(minutes since shift_start).
    """
    sh, sm = map(int, shift_start.split(":"))
    att = date.fromisoformat(att_date_str)
    shift_start_dt = datetime(att.year, att.month, att.day, sh, sm, 0, tzinfo=IST)

    diff = int((punch_in_utc.astimezone(IST) - shift_start_dt).total_seconds())

    if diff > 600:  # strictly more than 10 minutes
        return diff // 60
    return 0


def compute_overtime(
    punch_out_utc: datetime,
    shift_end: str,
    shift_start: str,
    att_date_str: str,
) -> int:
    """
    R3: overtime_minutes = floor(minutes between shift_end and punch_out),
    counted only when that is >= 30; otherwise 0.
    """
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))
    att = date.fromisoformat(att_date_str)

    # For overnight shifts, shift_end falls on the next calendar day
    if (eh * 60 + em) <= (sh * 60 + sm):
        end_date = att + timedelta(days=1)
    else:
        end_date = att

    shift_end_dt = datetime(
        end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST
    )
    diff = int((punch_out_utc.astimezone(IST) - shift_end_dt).total_seconds())

    if diff < 0:
        return 0
    minutes = diff // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_sec = int((punch_out - punch_in).total_seconds())
    d = Decimal(diff_sec) / Decimal(3600)
    places = Decimal("0.01")
    return float(d.quantize(places, rounding=ROUND_HALF_UP))


def format_employee(doc: dict) -> dict:
    """Serialize an employee document for the API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def format_history(history: list) -> list:
    """Convert history entries: datetimes → epoch ms."""
    result = []
    for entry in history:
        h = {
            "at": dt_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field, change in entry["changes"].items():
            if field in ("punch_in", "punch_out"):
                h["changes"][field] = {
                    "from": dt_to_epoch_ms(change["from"]),
                    "to": dt_to_epoch_ms(change["to"]),
                }
            else:
                h["changes"][field] = {"from": change["from"], "to": change["to"]}
        result.append(h)
    return result


def format_attendance(doc: dict) -> dict:
    """Serialize an attendance record for the API response."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": format_history(doc.get("history", [])),
    }


def count_working_days(start: date, end: date) -> int:
    """Count Mon-Fri days in [start, end] (R7)."""
    if start > end:
        return 0
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def _emp_code(cls, v):
        if not EMP_CODE_RE.match(v):
            raise ValueError("must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not 1 <= len(v) <= 100:
            raise ValueError("1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if len(v) > 120 or not EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def _department(cls, v):
        if not 1 <= len(v) <= 50:
            raise ValueError("1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _time(cls, v):
        if not TIME_RE.match(v):
            raise ValueError("must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def _date(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("must be YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Any] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Endpoints — system
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# --------------------------------------------------------------------------- #
# Endpoints — employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(
            422, detail=[{"msg": "shift_start must differ from shift_end"}]
        )

    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints — attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    # Validate status
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(
            422, detail=[{"msg": f"status must be one of {sorted(PRESENCE_STATUSES)}"}]
        )

    # Validate employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    # Parse / default timestamp
    if body.punched_at is not None:
        ms = validate_epoch_ms(body.punched_at)
        ts = epoch_ms_to_dt(ms)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # Determine attendance date (R1)
    att_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    # Compute late minutes (R2)
    late = compute_late(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    doc.pop("_id", None)
    return format_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    # Validate employee
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    # Parse / default timestamp
    if body.punched_at is not None:
        ms = validate_epoch_ms(body.punched_at)
        ts = epoch_ms_to_dt(ms)
    else:
        ts = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record whose punch_in is at or before punched_at (OpenAPI contract)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        # If employee has punch-ins but all are after ts, punched_at <= punch_in -> 422
        if db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}}
        ):
            raise HTTPException(
                422, detail=[{"msg": "punch_out must be after punch_in"}]
            )
        raise HTTPException(404, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="already punched out")

    # Validate timing constraints
    diff_sec = (ts - record["punch_in"]).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(
            422, detail=[{"msg": "punch_out must be after punch_in"}]
        )
    if diff_sec > 86400:
        raise HTTPException(
            422,
            detail=[{"msg": "punch_out must be within 24 hours of punch_in"}],
        )

    # Compute derived fields (R3-R5)
    wh = compute_work_hours(record["punch_in"], ts)
    ot = compute_overtime(ts, emp["shift_end"], emp["shift_start"], record["date"])
    hd = wh < 4.50  # R5

    # Atomic update (concurrency: exactly one punch-out succeeds)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, detail="already punched out")

    return format_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    df = validate_date_str(date_from)
    dt = validate_date_str(date_to)
    if df and dt and df > dt:
        raise HTTPException(
            422, detail=[{"msg": "date_from must be <= date_to"}]
        )
    if status and status not in ALL_STATUSES:
        raise HTTPException(
            422, detail=[{"msg": f"status must be one of {sorted(ALL_STATUSES)}"}]
        )

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [format_attendance(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints — regularize (manual correction)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    validate_date_str(date)

    # Validate reason / regularized_by lengths
    if not 5 <= len(body.reason) <= 200:
        raise HTTPException(
            422, detail=[{"msg": "reason must be 5-200 characters"}]
        )
    if not 1 <= len(body.regularized_by) <= 50:
        raise HTTPException(
            422, detail=[{"msg": "regularized_by must be 1-50 characters"}]
        )

    # Look up employee and record
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="no record for this date")

    provided = body.model_dump(exclude_unset=True)

    # Determine new field values
    new_status = provided.get("status", record["status"])
    if new_status not in ALL_STATUSES:
        raise HTTPException(
            422, detail=[{"msg": f"invalid status: {new_status}"}]
        )

    new_pi = record.get("punch_in")
    new_po = record.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in provided or "punch_out" in provided:
            raise HTTPException(
                422,
                detail=[{"msg": "cannot supply punch times with ABSENT/LEAVE"}],
            )
        new_pi = None
        new_po = None
    else:
        # Presence status
        if "punch_in" in provided:
            new_pi = epoch_ms_to_dt(validate_epoch_ms(provided["punch_in"]))
        if "punch_out" in provided:
            new_po = epoch_ms_to_dt(validate_epoch_ms(provided["punch_out"]))

        if new_pi is None:
            raise HTTPException(
                422, detail=[{"msg": "presence status requires punch_in"}]
            )

        # punch_in must stay on the record's attendance date (R1)
        computed_date = get_attendance_date(
            new_pi, emp["shift_start"], emp["shift_end"]
        )
        if computed_date != date:
            raise HTTPException(
                422,
                detail=[{"msg": "punch_in must stay on the record's attendance date"}],
            )

        # punch_out constraints
        if new_po is not None:
            diff_sec = (new_po - new_pi).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(
                    422, detail=[{"msg": "punch_out must be after punch_in"}]
                )
            if diff_sec > 86400:
                raise HTTPException(
                    422,
                    detail=[{"msg": "punch_out must be within 24 hours of punch_in"}],
                )

    # Compute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_wh = None
        new_late = 0
        new_ot = 0
        new_hd = False
    elif new_po is not None:
        new_late = compute_late(new_pi, emp["shift_start"], date)
        new_wh = compute_work_hours(new_pi, new_po)
        new_ot = compute_overtime(
            new_po, emp["shift_end"], emp["shift_start"], date
        )
        new_hd = new_wh < 4.50  # R5
    else:
        new_late = compute_late(new_pi, emp["shift_start"], date)
        new_wh = None
        new_ot = 0
        new_hd = False

    # Build the changes dict (only fields that actually changed)
    changes: dict = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}

    old_pi = record.get("punch_in")
    if new_pi != old_pi:
        changes["punch_in"] = {"from": old_pi, "to": new_pi}

    old_po = record.get("punch_out")
    if new_po != old_po:
        changes["punch_out"] = {"from": old_po, "to": new_po}

    old_wh = record.get("work_hours")
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}

    old_late = record.get("late_minutes", 0)
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}

    old_ot = record.get("overtime_minutes", 0)
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}

    old_hd = record.get("half_day", False)
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail=[{"msg": "no changes"}])

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_doc = {
        "status": new_status,
        "punch_in": new_pi,
        "punch_out": new_po,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }

    # Optimistic concurrency: history length must match
    hist_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": hist_len}},
        {"$set": update_doc, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, detail="record was modified concurrently")

    return format_attendance(result)


# --------------------------------------------------------------------------- #
# Pipelines — analytics
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, first_str: str, last_str: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
        {
            "$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "_half": {"$ifNull": ["$half_day", False]},
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_ot": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$addFields": {
                "dow": {"$dayOfWeek": "$date_obj"},  # 1=Sun, 2=Mon ... 6=Fri, 7=Sat
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present",
                                    {"$gte": ["$dow", 2]},
                                    {"$lte": ["$dow", 6]},
                                ]
                            },
                            {"$cond": ["$_half", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$_late"},
                "total_overtime_minutes": {"$sum": "$_ot"},
            }
        },
    ]


def build_department_summary_pipeline(
    first_str: str,
    last_str: str,
    department: Optional[str] = None,
) -> list:
    """
    Single cross-collection aggregation rooted in employees, looking up into
    attendance_logs. Computes headcount and attendance metrics simultaneously.
    Employees with zero logs are preserved.
    """
    match_stage: dict = {"joined_on": {"$lte": last_str}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_str]},
                                    {"$lte": ["$date", last_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "date_obj": {
                                "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                            },
                            "is_presence": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        }
                    },
                    {
                        "$addFields": {
                            "dow": {"$dayOfWeek": "$date_obj"},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "emp_present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$$this.is_presence",
                                                {"$gte": ["$$this.dow", 2]},
                                                {"$lte": ["$$this.dow", 6]},
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$$this.half_day", False]}, True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_wh_sum": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$$this.is_presence",
                                                {"$ne": [{"$ifNull": ["$$this.work_hours", None]}, None]},
                                            ]
                                        },
                                        "$$this.work_hours",
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_wh_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$$this.is_presence",
                                                {"$ne": [{"$ifNull": ["$$this.work_hours", None]}, None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_late_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                            ]
                        },
                    }
                },
                "emp_total_late": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$ifNull": ["$$this.late_minutes", 0]},
                            ]
                        },
                    }
                },
                "emp_leave_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                            ]
                        },
                    }
                },
                "emp_on_duty_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "wh_sum": {"$sum": "$emp_wh_sum"},
                "wh_count": {"$sum": "$emp_wh_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$wh_count", 0]},
                        mongo_round_half_up_expr({"$divide": ["$wh_sum", "$wh_count"]}, 2),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(first_str: str, last_str: str, limit: int = 10, department: Optional[str] = None) -> list:
    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": first_str, "$lte": last_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            # Standard competition ranking (1, 2, 2, 4) on total_late_minutes desc
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    department: str,
    date_from: str,
    date_to: str,
    emp_codes: list,
) -> list:
    from_d = date.fromisoformat(date_from)
    to_d = date.fromisoformat(date_to)
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    to_dt_excl = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                },
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "_half": {"$ifNull": ["$half_day", False]},
                "_late": {"$ifNull": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date_obj",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            "$is_present",
                            {"$cond": ["$_half", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_excl]},
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "dow": {"$dayOfWeek": "$_id"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$dow", 2]},
                        {"$lte": ["$dow", 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_d": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc_docs.hc"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        mongo_round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        mongo_round_half_up_expr("$moving_avg_7d_raw", 4),
                        None,
                    ]
                }
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Endpoints — analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    first_day, last_day = parse_month(month)
    first_str, last_str = first_day.isoformat(), last_day.isoformat()

    # Working days: Mon-Fri in the month, starting from joined_on if mid-month (R7)
    joined = date.fromisoformat(emp["joined_on"])
    start_from = max(first_day, joined)
    working_days = count_working_days(start_from, last_day)

    pipeline = build_employee_monthly_pipeline(emp_code, first_str, last_str)
    rows = list(db.attendance_logs.aggregate(pipeline))

    if rows:
        r = rows[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    att_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = parse_month(month)
    first_str, last_str = first_day.isoformat(), last_day.isoformat()

    pipeline = build_department_summary_pipeline(first_str, last_str, department)
    items = list(db.employees.aggregate(pipeline))

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = parse_month(month)
    first_str, last_str = first_day.isoformat(), last_day.isoformat()

    pipeline = build_late_leaderboard_pipeline(first_str, last_str, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    # Validate department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="department not found")

    # Parse and validate date range
    from_d = validate_date_str(date_from)
    to_d = validate_date_str(date_to)

    if to_d < from_d:
        raise HTTPException(422, detail=[{"msg": "to must be >= from"}])
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(422, detail=[{"msg": "range must not exceed 92 days"}])

    # Fetch department employees
    employees = list(
        db.employees.find(
            {"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}
        )
    )
    emp_codes = [e["emp_code"] for e in employees]

    pipeline = build_department_trend_pipeline(department, date_from, date_to, emp_codes)
    rows = list(db.attendance_logs.aggregate(pipeline))

    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Endpoints — admin / explain
# --------------------------------------------------------------------------- #
EXPLAINABLE = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    range_from: Optional[str] = Query(None, alias="from"),
    range_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAINABLE:
        raise HTTPException(
            422,
            detail=[{"msg": f"endpoint must be one of {sorted(EXPLAINABLE)}"}],
        )

    # ---- attendance_list (find) ----
    if endpoint == "attendance_list":
        df = validate_date_str(date_from)
        dt = validate_date_str(date_to)
        if df and dt and df > dt:
            raise HTTPException(
                422, detail=[{"msg": "date_from must be <= date_to"}]
            )
        if status and status not in ALL_STATUSES:
            raise HTTPException(
                422, detail=[{"msg": f"status must be one of {sorted(ALL_STATUSES)}"}]
            )

        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_bson(explain)}

    # ---- employee_monthly (aggregation) ----
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422, detail=[{"msg": "emp_code and month are required"}]
            )
        first, last = parse_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_bson(explain)}

    # ---- department_summary (aggregation) ----
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month is required"}])
        first, last = parse_month(month)
        pipeline = build_department_summary_pipeline(first.isoformat(), last.isoformat(), department)
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_bson(explain)}

    # ---- late_leaderboard (aggregation) ----
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month is required"}])
        first, last = parse_month(month)
        pipeline = build_late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_bson(explain)}

    # ---- department_trend (aggregation) ----
    if endpoint == "department_trend":
        if not department or not range_from or not range_to:
            raise HTTPException(
                422, detail=[{"msg": "department, from, and to are required"}]
            )
        from_d = validate_date_str(range_from)
        to_d = validate_date_str(range_to)
        if to_d < from_d:
            raise HTTPException(422, detail=[{"msg": "to must be >= from"}])
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(422, detail=[{"msg": "range must not exceed 92 days"}])

        emps = list(
            db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
        )
        codes = [e["emp_code"] for e in emps]
        pipeline = build_department_trend_pipeline(department, range_from, range_to, codes)
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_bson(explain)}
