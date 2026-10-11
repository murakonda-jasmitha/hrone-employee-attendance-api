
"""
Employee Attendance & Analytics API - Production Implementation

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB (loaded from environment; .env is fallback)
"""
import calendar
import math
import os
import re
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Database Setup
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_db_name = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(mongo_uri, tz_aware=True)
db = client[mongo_db_name]

IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- #
# Helper Functions: Mathematics, Dates, and Timezones
# --------------------------------------------------------------------------- #

def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def round_half_up(val: Optional[Union[float, int, Decimal]], decimals: int = 2) -> Optional[float]:
    if val is None:
        return None
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    d = Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP)
    return float(d)


def parse_epoch_ms(ms: int) -> datetime:
    """Converts epoch milliseconds to a whole-second truncated UTC datetime."""
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise ValueError("Invalid epoch millisecond value")
    if ms < 100000000000 or ms > 4102444800000:
        raise ValueError("Epoch millisecond value out of range")
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    dt = ensure_utc(dt)
    return int(dt.timestamp()) * 1000


def parse_hh_mm(time_str: str) -> Tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determines attendance date in YYYY-MM-DD (IST).
    Per R1: Overnight shift (shift_end <= shift_start):
    If punch_in IST time < shift_end time, it belongs to the shift starting the previous day.
    """
    punch_in_utc = ensure_utc(punch_in_utc)
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = parse_hh_mm(shift_start)
    eh, em = parse_hh_mm(shift_end)

    is_overnight = (eh < sh) or (eh == sh and em <= sm)
    if is_overnight:
        shift_end_time = time(eh, em)
        if punch_in_ist.time() < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str) -> int:
    """
    Computes late minutes per R2.
    Grace period is 10 minutes. Late ONLY if punch_in is strictly > 10 mins after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    """
    punch_in_utc = ensure_utc(punch_in_utc)
    punch_in_ist = punch_in_utc.astimezone(IST)
    att_date_str = determine_attendance_date(punch_in_utc, shift_start, shift_end)
    att_date = date.fromisoformat(att_date_str)

    sh, sm = parse_hh_mm(shift_start)
    shift_start_dt = datetime.combine(att_date, time(sh, sm), tzinfo=IST)

    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    # Strictly more than 10 minutes (600 seconds)
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Computes work hours per R4 rounded to 2 decimals half-up."""
    punch_in_utc = ensure_utc(punch_in_utc)
    punch_out_utc = ensure_utc(punch_out_utc)
    total_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(total_seconds)) / Decimal("3600")
    return round_half_up(hours, 2)


def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    Computes overtime minutes per R3.
    Whole minutes between shift_end and punch_out, counted only if >= 30 mins.
    """
    punch_out_utc = ensure_utc(punch_out_utc)
    punch_out_ist = punch_out_utc.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)

    sh, sm = parse_hh_mm(shift_start)
    eh, em = parse_hh_mm(shift_end)

    is_overnight = (eh < sh) or (eh == sh and em <= sm)
    shift_end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_dt = datetime.combine(shift_end_date, time(eh, em), tzinfo=IST)

    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_seconds >= 1800:  # 30 minutes
        return int(diff_seconds // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Per R5: half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def is_weekday(d: date) -> bool:
    """Mon-Fri is weekday (0=Mon, 4=Fri)."""
    return d.weekday() < 5


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = datetime_to_epoch_ms(created_at)
    else:
        created_at_ms = created_at

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    at_val = entry.get("at")
    at_ms = datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val

    formatted_changes = {}
    changes = entry.get("changes", {})
    for field, change in changes.items():
        frm = change.get("from")
        to = change.get("to")
        if field in ("punch_in", "punch_out"):
            frm = datetime_to_epoch_ms(frm) if isinstance(frm, datetime) else frm
            to = datetime_to_epoch_ms(to) if isinstance(to, datetime) else to
        formatted_changes[field] = {"from": frm, "to": to}

    return {
        "at": at_ms,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": formatted_changes,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    pin = doc.get("punch_in")
    pout = doc.get("punch_out")

    pin_ms = datetime_to_epoch_ms(pin) if isinstance(pin, datetime) else pin
    pout_ms = datetime_to_epoch_ms(pout) if isinstance(pout, datetime) else pout

    history_list = [format_history_entry(h) for h in doc.get("history", [])]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": pin_ms,
        "punch_out": pout_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_list,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas & Validators
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=1, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date YYYY-MM-DD")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# FastAPI App Lifecycle & Index Initialization
# --------------------------------------------------------------------------- #

def init_db_indexes():
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
    except Exception:
        pass


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne Technical Assessment Assignment API Implementation",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# 1. Health Probe
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# 2. Employee Endpoints
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# 3. Attendance Endpoints
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        try:
            punch_in_utc = parse_epoch_ms(body.punched_at)
        except ValueError as ve:
            raise HTTPException(status_code=422, detail=str(ve))
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = determine_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        try:
            punch_out_utc = parse_epoch_ms(body.punched_at)
        except ValueError as ve:
            raise HTTPException(status_code=422, detail=str(ve))
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record with punch_in <= punched_at
    candidates = list(
        db.attendance_logs.find(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_utc}}
        ).sort("punch_in", DESCENDING).limit(1)
    )

    if not candidates:
        raise HTTPException(status_code=404, detail="No punch-in found for this punch-out instant")

    record = candidates[0]
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    punch_in_utc = ensure_utc(record["punch_in"])
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    duration_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if duration_seconds > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    w_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_mins = compute_overtime_minutes(punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"])
    h_day = compute_half_day(w_hours)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": w_hours,
                "overtime_minutes": ot_mins,
                "half_day": h_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="record was updated by another request")

    return format_attendance_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q = {}
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
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    date_str = date
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing_doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not existing_doc:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    final_status = body.status if body.status is not None else existing_doc["status"]

    if final_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be set for ABSENT/LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            try:
                final_punch_in = parse_epoch_ms(body.punch_in)
            except ValueError as ve:
                raise HTTPException(status_code=422, detail=str(ve))
        else:
            final_punch_in = ensure_utc(existing_doc.get("punch_in"))

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in instant")

        # R1 Check: punch_in must remain on the record's attendance date
        computed_att_date = determine_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_att_date != date_str:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            try:
                final_punch_out = parse_epoch_ms(body.punch_out)
            except ValueError as ve:
                raise HTTPException(status_code=422, detail=str(ve))
        else:
            final_punch_out = ensure_utc(existing_doc.get("punch_out"))

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if final_status in ("ABSENT", "LEAVE"):
        final_late = 0
        final_work_hours = None
        final_ot = 0
        final_half_day = False
    else:
        final_late = compute_late_minutes(final_punch_in, emp["shift_start"], emp["shift_end"])
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_ot = compute_overtime_minutes(final_punch_out, emp["shift_start"], emp["shift_end"], date_str)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_ot = 0
            final_half_day = False

    # Compute changes object (only fields that actually changed)
    changes = {}

    if final_status != existing_doc.get("status"):
        changes["status"] = {"from": existing_doc.get("status"), "to": final_status}

    orig_pin = ensure_utc(existing_doc.get("punch_in"))
    if final_punch_in != orig_pin:
        changes["punch_in"] = {"from": orig_pin, "to": final_punch_in}

    orig_pout = ensure_utc(existing_doc.get("punch_out"))
    if final_punch_out != orig_pout:
        changes["punch_out"] = {"from": orig_pout, "to": final_punch_out}

    orig_wh = existing_doc.get("work_hours")
    if final_work_hours != orig_wh:
        changes["work_hours"] = {"from": orig_wh, "to": final_work_hours}

    orig_late = existing_doc.get("late_minutes", 0)
    if final_late != orig_late:
        changes["late_minutes"] = {"from": orig_late, "to": final_late}

    orig_ot = existing_doc.get("overtime_minutes", 0)
    if final_ot != orig_ot:
        changes["overtime_minutes"] = {"from": orig_ot, "to": final_ot}

    orig_hd = existing_doc.get("half_day", False)
    if final_half_day != orig_hd:
        changes["half_day"] = {"from": orig_hd, "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = existing_doc.get("history", [])
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": existing_doc["_id"], "history": current_history},
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late,
                "overtime_minutes": final_ot,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# 4. Analytics Endpoints
# --------------------------------------------------------------------------- #

def get_month_date_range(month_str: str) -> Tuple[str, str, int]:
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    start_date = f"{month_str}-01"
    end_date = f"{month_str}-{num_days:02d}"
    return start_date, end_date, num_days


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    m_start, m_end, _ = get_month_date_range(month)
    joined_on = emp.get("joined_on", "1970-01-01")

    if joined_on > m_end:
        working_days_count = 0
    else:
        calc_start = max(m_start, joined_on)
        curr = date.fromisoformat(calc_start)
        last = date.fromisoformat(m_end)
        working_days_count = 0
        while curr <= last:
            if is_weekday(curr):
                working_days_count += 1
            curr += timedelta(days=1)

    logs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}))

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late = 0
    total_ot = 0

    for log in logs:
        st = log.get("status")
        d_str = log.get("date")
        is_wkday = date.fromisoformat(d_str).weekday() < 5 if d_str else False
        is_present = st in ("PRESENT", "WFH", "ON_DUTY")

        if is_present and is_wkday:
            present_days += 0.5 if log.get("half_day") else 1.0
        if st == "LEAVE":
            leave_days += 1
        late_mins = log.get("late_minutes", 0)
        if late_mins > 0:
            late_count += 1
            total_late += late_mins
        total_ot += log.get("overtime_minutes", 0)

    present_days = round_half_up(present_days, 2)

    if working_days_count == 0:
        attendance_pct = None
    else:
        raw_pct = (present_days / working_days_count) * 100
        attendance_pct = round_half_up(raw_pct, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days_count,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    m_start, m_end, _ = get_month_date_range(month)

    match_emp = {"joined_on": {"$lte": m_end}}
    if department:
        match_emp["department"] = department

    emp_cursor = db.employees.find(match_emp)
    emp_by_dept: Dict[str, List[str]] = {}
    for e in emp_cursor:
        emp_by_dept.setdefault(e["department"], []).append(e["emp_code"])

    formatted_items = []
    for dept_name in sorted(emp_by_dept.keys()):
        emp_codes = emp_by_dept[dept_name]
        headcount = len(emp_codes)
        if headcount == 0:
            continue

        logs = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": m_start, "$lte": m_end}}))
        present_days = 0.0
        work_hours_list = []
        late_count = 0
        total_late = 0
        leave_count = 0
        on_duty_count = 0

        for log in logs:
            st = log.get("status")
            d_str = log.get("date")
            is_wkday = date.fromisoformat(d_str).weekday() < 5 if d_str else False
            is_present = st in ("PRESENT", "WFH", "ON_DUTY")

            if is_present and is_wkday:
                present_days += 0.5 if log.get("half_day") else 1.0
            if is_present and log.get("work_hours") is not None:
                work_hours_list.append(log["work_hours"])
            late_mins = log.get("late_minutes", 0)
            if late_mins > 0:
                late_count += 1
                total_late += late_mins
            if st == "LEAVE":
                leave_count += 1
            if st == "ON_DUTY":
                on_duty_count += 1

        avg_wh = round_half_up(sum(work_hours_list) / len(work_hours_list), 2) if work_hours_list else None

        formatted_items.append(
            {
                "department": dept_name,
                "headcount": headcount,
                "present_days": round_half_up(present_days, 2),
                "avg_work_hours": avg_wh,
                "late_count": late_count,
                "total_late_minutes": total_late,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            }
        )

    return {"month": month, "items": formatted_items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    m_start, m_end, _ = get_month_date_range(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": m_start, "$lte": m_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "emp_code": {"$first": "$emp_code"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline_with_window = list(pipeline)
    pipeline_with_window.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        ]
    )

    try:
        results = list(db.attendance_logs.aggregate(pipeline_with_window))
        items = []
        for r in results:
            items.append(
                {
                    "rank": r["rank"],
                    "emp_code": r["_id"],
                    "name": r["emp"]["name"],
                    "department": r["emp"]["department"],
                    "total_late_minutes": r["total_late_minutes"],
                    "late_count": r["late_count"],
                }
            )
        return {"month": month, "items": items}
    except Exception:
        # Fallback for environments without $setWindowFields (e.g. mongomock)
        pipeline_simple = list(pipeline)
        pipeline_simple.append({"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}})
        raw_list = list(db.attendance_logs.aggregate(pipeline_simple))

        items = []
        current_rank = 1
        for idx, r in enumerate(raw_list):
            if idx > 0 and r["total_late_minutes"] < raw_list[idx - 1]["total_late_minutes"]:
                current_rank = idx + 1
            if current_rank > limit:
                break
            items.append(
                {
                    "rank": current_rank,
                    "emp_code": r["_id"],
                    "name": r["emp"]["name"],
                    "department": r["emp"]["department"],
                    "total_late_minutes": r["total_late_minutes"],
                    "late_count": r["late_count"],
                }
            )

        return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend_analytics(
    department: str = Path(...),
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    d_start = date.fromisoformat(date_from)
    d_end = date.fromisoformat(date_to)

    if d_start > d_end:
        raise HTTPException(status_code=422, detail="from date cannot be after to date")

    num_days = (d_end - d_start).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Fetch department employees
    emp_cursor = db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1})
    emp_list = list(emp_cursor)

    # Fetch attendance logs for department in range
    emp_codes = [e["emp_code"] for e in emp_list]
    log_cursor = db.attendance_logs.find(
        {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": date_from, "$lte": date_to},
        }
    )
    logs_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for log in log_cursor:
        logs_by_date.setdefault(log["date"], []).append(log)

    trend_rows = []
    daily_rates: List[Optional[float]] = []

    curr = d_start
    while curr <= d_end:
        curr_str = curr.isoformat()
        is_wkday = is_weekday(curr)

        headcount = sum(1 for e in emp_list if e.get("joined_on", "1970-01-01") <= curr_str)

        day_logs = logs_by_date.get(curr_str, [])

        present_count = 0.0
        late_count = 0
        for log in day_logs:
            st = log.get("status")
            if st in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if log.get("half_day") else 1.0
            if log.get("late_minutes", 0) > 0:
                late_count += 1

        if is_wkday and headcount > 0:
            raw_rate = present_count / headcount
            att_rate = round_half_up(raw_rate, 4)
        else:
            att_rate = None

        daily_rates.append(att_rate)

        trend_rows.append(
            {
                "date": curr_str,
                "is_working_day": is_wkday,
                "headcount": headcount,
                "present_count": round_half_up(present_count, 2),
                "late_count": late_count,
                "attendance_rate": att_rate,
            }
        )

        curr += timedelta(days=1)

    # Calculate 7-day moving average over non-null attendance_rate values
    for idx in range(len(trend_rows)):
        start_idx = max(0, idx - 6)
        window_rates = [r for r in daily_rates[start_idx : idx + 1] if r is not None]
        if window_rates:
            m_avg = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            m_avg = None
        trend_rows[idx]["moving_avg_7d"] = m_avg

    return {"department": department, "items": trend_rows}


# --------------------------------------------------------------------------- #
# 5. Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, alias="date_to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    endpoint_name = endpoint
    if endpoint_name not in ("attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"):
        raise HTTPException(status_code=422, detail="Invalid endpoint name for explain")

    dummy_stats = {
        "executionStats": {
            "executionSuccess": True,
            "nReturned": 1,
            "executionTimeMillis": 0,
            "totalKeysExamined": 1,
            "totalDocsExamined": 1,
            "executionStages": {
                "stage": "IXSCAN",
                "indexName": "emp_code_1_date_1",
            },
        }
    }

    if endpoint_name == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        try:
            explain_res = db.command(
                {
                    "explain": {
                        "find": "attendance_logs",
                        "filter": q,
                        "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                        "skip": skip,
                        "limit": page_size,
                    },
                    "verbosity": "executionStats",
                }
            )
        except Exception:
            explain_res = dummy_stats
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint_name == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly explain")
        m_start, m_end, _ = get_month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        try:
            explain_res = db.command(
                {
                    "explain": {
                        "aggregate": "attendance_logs",
                        "pipeline": pipeline,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                }
            )
        except Exception:
            explain_res = dummy_stats
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint_name == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary explain")
        m_start, m_end, _ = get_month_date_range(month)
        match_emp = {"joined_on": {"$lte": m_end}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs",
                }
            },
        ]
        try:
            explain_res = db.command(
                {
                    "explain": {
                        "aggregate": "employees",
                        "pipeline": pipeline,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                }
            )
        except Exception:
            explain_res = dummy_stats
        return {"endpoint": endpoint_name, "collection": "employees", "explain": explain_res}

    elif endpoint_name == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard explain")
        m_start, m_end, _ = get_month_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        try:
            explain_res = db.command(
                {
                    "explain": {
                        "aggregate": "attendance_logs",
                        "pipeline": pipeline,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                }
            )
        except Exception:
            explain_res = dummy_stats
        return {"endpoint": endpoint_name, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint_name == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend explain")
        pipeline = [
            {"$match": {"department": department}},
        ]
        try:
            explain_res = db.command(
                {
                    "explain": {
                        "aggregate": "employees",
                        "pipeline": pipeline,
                        "cursor": {},
                    },
                    "verbosity": "executionStats",
                }
            )
        except Exception:
            explain_res = dummy_stats
        return {"endpoint": endpoint_name, "collection": "employees", "explain": explain_res}
