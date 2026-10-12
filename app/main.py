"""Employee Attendance & Analytics API for the HROne engineering assignment."""

import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # Real process environment variables take precedence over .env.
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
if not MONGO_URI:
    # Do not fail at import time: /health can report the service as unavailable.
    client = None
    db = None
else:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Validation models
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("name", "department")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return validate_epoch_ms(value)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value):
        return validate_epoch_ms(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("reason", "regularized_by")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_timestamps(cls, value):
        return validate_epoch_ms(value)


# ---------------------------------------------------------------------------
# Helpers: time, rounding, serialization, validation
# ---------------------------------------------------------------------------
def validate_epoch_ms(value):
    if value is None:
        return None
    # Reject booleans, floats, numeric strings, and values in seconds.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timestamp must be an integer epoch-millisecond value")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise ValueError("timestamp is outside the supported epoch-millisecond range")
    return value


def to_utc_naive(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).replace(microsecond=0, tzinfo=None)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    value = value.replace(microsecond=0)
    return int(value.timestamp()) * 1000


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def api_record(doc: dict) -> dict:
    """Convert MongoDB datetimes to epoch milliseconds; never expose Mongo _id."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = to_epoch_ms(out.get("punch_in"))
    out["punch_out"] = to_epoch_ms(out.get("punch_out"))
    out["history"] = out.get("history") or []
    for entry in out["history"]:
        entry["at"] = to_epoch_ms(entry.get("at"))
        changes = entry.get("changes") or {}
        for field in ("punch_in", "punch_out"):
            if field in changes:
                changes[field]["from"] = to_epoch_ms(changes[field].get("from"))
                changes[field]["to"] = to_epoch_ms(changes[field].get("to"))
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    out.setdefault("work_hours", None)
    return out


def api_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_epoch_ms(out.get("created_at"))
    return out


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_ist(punch_in)
    start = datetime.strptime(shift_start, "%H:%M").time()
    end = datetime.strptime(shift_end, "%H:%M").time()
    if end <= start and local.time().replace(tzinfo=None) < end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_start_datetime(attendance_date: str, shift_start: str) -> datetime:
    d = date.fromisoformat(attendance_date)
    t = datetime.strptime(shift_start, "%H:%M").time()
    return datetime.combine(d, t, tzinfo=IST)


def shift_end_datetime(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    d = date.fromisoformat(attendance_date)
    start = datetime.strptime(shift_start, "%H:%M").time()
    end = datetime.strptime(shift_end, "%H:%M").time()
    if end <= start:
        d += timedelta(days=1)
    return datetime.combine(d, end, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    elapsed = (as_ist(punch_in) - shift_start_datetime(attendance_date, shift_start)).total_seconds()
    if elapsed <= 600:
        return 0
    return max(0, int(elapsed // 60))


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return float((Decimal(str(seconds)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    elapsed = (as_ist(punch_out) - shift_end_datetime(attendance_date, shift_start, shift_end)).total_seconds()
    minutes = int(elapsed // 60)
    return minutes if minutes >= 30 else 0


def parse_iso_date(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{label} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{label} must use YYYY-MM-DD format")
    return parsed


def month_bounds(month: str) -> tuple[str, str, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM format")
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat(), first, last


def working_days_between(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def require_db():
    if db is None or client is None:
        raise HTTPException(status_code=503, detail="MongoDB is not configured")


# ---------------------------------------------------------------------------
# Indexes and health
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    if db is None:
        return
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_employee_attendance_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="attendance_employee_date")
    # Supports finding the latest punch-in for an employee efficiently.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch_in")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="attendance_date_late")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_employee")


@app.get("/health")
def health():
    if client is None:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    require_db()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    doc["_id"] = result.inserted_id
    return api_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    require_db()
    q = {"department": department} if department is not None else {}
    try:
        total = db.employees.count_documents(q)
        docs = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    return {"items": [api_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance punch-in / punch-out / listing / correction
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    require_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = to_utc_naive(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    attendance_date = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    doc["_id"] = result.inserted_id
    return api_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    require_db()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = to_utc_naive(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    # Find the latest punch-in no later than the supplied punch-out. The date is the
    # attendance date, so this also finds overnight shifts whose punch-out is next day.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")
    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    update = {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": work_hours < 4.50}
    try:
        result = db.attendance_logs.update_one(
            {"_id": record["_id"], "punch_out": None},
            {"$set": update},
        )
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return api_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    require_db()
    if date_from:
        parse_iso_date(date_from, "date_from")
    if date_to:
        parse_iso_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    try:
        total = db.attendance_logs.count_documents(q)
        docs = list(db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    return {"items": [api_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    require_db()
    parse_iso_date(date)
    if not db.employees.find_one({"emp_code": emp_code}):
        raise HTTPException(status_code=404, detail="Employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    emp = db.employees.find_one({"emp_code": emp_code})
    current_status = old.get("status")
    status = body.status if body.status is not None else current_status
    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in body.model_fields_set or "punch_out" in body.model_fields_set:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times")
        punch_in = punch_out = None
    else:
        raw_in = body.punch_in if "punch_in" in body.model_fields_set else to_epoch_ms(old.get("punch_in"))
        raw_out = body.punch_out if "punch_out" in body.model_fields_set else to_epoch_ms(old.get("punch_out"))
        if raw_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        punch_in = to_utc_naive(raw_in)
        punch_out = to_utc_naive(raw_out) if raw_out is not None else None
        if attendance_date_for(punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must belong to the record attendance date")
        if punch_out is not None:
            elapsed = (punch_out - punch_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(punch_in, emp["shift_start"], date) if punch_in else 0
    if punch_in and punch_out:
        work = compute_work_hours(punch_in, punch_out)
        overtime = compute_overtime(punch_out, emp["shift_end"], date, emp["shift_start"])
        half = work < 4.50
    else:
        work, overtime, half = None, 0, False
    proposed = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half,
    }
    # Missing derived fields in seeded records have defined semantic defaults:
    # late/overtime = 0 and half_day = false. Do not report these as changes
    # when a regularization only modifies another field.
    old_values = {k: old.get(k) for k in proposed}
    old_values["late_minutes"] = old.get("late_minutes", 0)
    old_values["overtime_minutes"] = old.get("overtime_minutes", 0)
    old_values["half_day"] = old.get("half_day", False)
    changes = {}
    for field, new_value in proposed.items():
        old_value = old_values.get(field)
        if field in ("punch_in", "punch_out"):
            equal = (old_value == new_value) or (old_value is None and new_value is None)
        elif field == "work_hours" and old_value is not None and new_value is not None:
            equal = Decimal(str(old_value)) == Decimal(str(new_value))
        else:
            equal = old_value == new_value
        if not equal:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="Request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0, tzinfo=None), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-set on the previous history prevents concurrent PATCH calls from
    # silently overwriting each other's audit entries. Missing history is treated as [].
    old_history = old.get("history") or []
    try:
        result = db.attendance_logs.update_one(
            {"_id": old["_id"], "$expr": {"$eq": [{"$ifNull": ["$history", []]}, old_history]}},
            {"$set": proposed, "$push": {"history": entry}},
        )
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Record changed concurrently; reload and retry")
    updated = db.attendance_logs.find_one({"_id": old["_id"]})
    return api_record(updated)


# ---------------------------------------------------------------------------
# Shared aggregation pipeline builders (also used by explain endpoint)
# ---------------------------------------------------------------------------
def attendance_filter_pipeline(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20):
    match = {}
    if emp_code:
        match["emp_code"] = emp_code
    if date_from or date_to:
        match["date"] = {}
        if date_from:
            match["date"]["$gte"] = date_from
        if date_to:
            match["date"]["$lte"] = date_to
    if status:
        match["status"] = status
    return match, [{"$match": match}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}, {"$project": {"_id": 0}}]


def monthly_log_pipeline(emp_code: str, first: str, last: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$in": ["$_weekday", [2, 3, 4, 5, 6]]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None):
    first, last, _, _ = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]}, {"$lte": ["$date", last]}]}}},
            {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$in": ["$logs._weekday", [2, 3, 4, 5, 6]]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [
                {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [
                {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    first, last, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def trend_pipeline(department: str, start: date, end: date):
    # Generate the requested calendar rows inside MongoDB, then use indexed lookups.
    days = (end - start).days + 1
    return [
        {"$documents": [{"_id": 1}]},
        {"$project": {"days": {"$map": {"input": {"$range": [0, days]}, "as": "n", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat(), "timezone": "+05:30"}}, "unit": "day", "amount": "$$n"}}, "format": "%Y-%m-%d", "timezone": "+05:30"}}}}}},
        {"$unwind": "$days"},
        {"$project": {"date": "$days", "_id": 0}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "count"}
        ], "as": "hc"}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "day_logs"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}, "present_count": {"$ifNull": [{"$arrayElemAt": ["$day_logs.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$day_logs.late_count", 0]}, 0]}, "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$addFields": {"is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}, "attendance_rate": {"$cond": [{"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}, {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


# ---------------------------------------------------------------------------
# Analytics endpoints: calculations over stored values, using MongoDB pipelines
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    require_db()
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    first_s, last_s, first, last = month_bounds(month)
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = working_days_between(start, last) if start <= last else 0
    try:
        result = list(db.attendance_logs.aggregate(monthly_log_pipeline(emp_code, first_s, last_s), hint="attendance_employee_date"))
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    data = result[0] if result else {}
    present = round_half_up(data.get("present_days", 0), 2)
    pct = round_half_up(present / working_days * 100, 4) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present,
            "leave_days": data.get("leave_days", 0), "late_count": data.get("late_count", 0),
            "total_late_minutes": data.get("total_late_minutes", 0), "total_overtime_minutes": data.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    require_db()
    month_bounds(month)
    try:
        items = list(db.employees.aggregate(department_summary_pipeline(month, department), hint="department_joined_on"))
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    # Round all reported numeric values consistently (R8).
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    require_db()
    month_bounds(month)
    try:
        items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department), hint="attendance_date_late"))
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database operation failed") from exc
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    require_db()
    if from_date is None or to_date is None:
        raise HTTPException(status_code=422, detail="from and to are required")
    start = parse_iso_date(from_date, "from")
    end = parse_iso_date(to_date, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")
    try:
        items = list(db.aggregate(trend_pipeline(department, start, end)))
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database aggregation failed") from exc
    # Some MongoDB versions do not expose $documents through a database aggregate.
    # The intended contract requires server-side gap filling; errors are not hidden.
    for item in items:
        if item.get("attendance_rate") is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------
def explain_command(collection, pipeline: list, hint: Optional[str] = None):
    command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    if hint:
        command["hint"] = hint
    return db.command({"explain": command, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    require_db()
    try:
        if endpoint == "attendance_list":
            if date_from: parse_iso_date(date_from, "date_from")
            if date_to: parse_iso_date(date_to, "date_to")
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
            _, pipeline = attendance_filter_pipeline(emp_code, date_from, date_to, status, page, page_size)
            result = explain_command("attendance_logs", pipeline, "attendance_date_employee")
            collection = "attendance_logs"
        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required")
            first, last, _, _ = month_bounds(month)
            result = explain_command("attendance_logs", monthly_log_pipeline(emp_code, first, last), "attendance_employee_date")
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            month_bounds(month)
            result = explain_command("employees", department_summary_pipeline(month, department), "department_joined_on")
            collection = "employees"
        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            month_bounds(month)
            result = explain_command("attendance_logs", leaderboard_pipeline(month, limit, department), "attendance_date_late")
            collection = "attendance_logs"
        else:
            if not department or not from_date or not to_date:
                raise HTTPException(status_code=422, detail="department, from and to are required")
            start = parse_iso_date(from_date, "from")
            end = parse_iso_date(to_date, "to")
            if end < start or (end - start).days + 1 > 92:
                raise HTTPException(status_code=422, detail="invalid trend date range")
            result = explain_command(1, trend_pipeline(department, start, end))
            collection = "attendance_logs"
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Could not explain query") from exc
    return {"endpoint": endpoint, "collection": collection, "explain": result}
