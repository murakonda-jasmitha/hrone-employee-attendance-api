"""
Test suite for HROne Employee Attendance & Analytics API
"""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
import mongomock

import app.main as main_module

# Replace real Mongo db with mongomock client for testing
mock_client = mongomock.MongoClient()
mock_db = mock_client["test_db"]

main_module.db = mock_db
main_module.client = mock_client

test_client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def setup_db():
    mock_db.employees.delete_many({})
    mock_db.attendance_logs.delete_many({})
    main_module.init_db_indexes()
    yield


def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_employee():
    # Valid employee
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = test_client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)

    # Duplicate emp_code -> 409
    res_dup = test_client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # Same shift_start and shift_end -> 422
    payload_bad = dict(payload, emp_code="EMP0002", shift_end="09:30")
    res_bad = test_client.post("/employees", json=payload_bad)
    assert res_bad.status_code == 422


def test_list_employees():
    for i in range(1, 6):
        emp_code = f"EMP000{i}"
        dept = "Engineering" if i <= 3 else "HR"
        test_client.post(
            "/employees",
            json={
                "emp_code": emp_code,
                "name": f"Employee {i}",
                "email": f"emp{i}@example.com",
                "department": dept,
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

    res = test_client.get("/employees?page=1&page_size=2")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert data["items"][1]["emp_code"] == "EMP0002"

    # Filter department
    res_dept = test_client.get("/employees?department=HR")
    assert res_dept.status_code == 200
    data_dept = res_dept.json()
    assert data_dept["total"] == 2
    assert data_dept["items"][0]["emp_code"] == "EMP0004"


def test_punch_in_and_out_boundaries():
    # Setup employee
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # 1. Punch in exactly at 09:40:00 IST (10 mins grace) -> late_minutes = 0
    # 2026-07-06 09:40:00 IST is 2026-07-06 04:10:00 UTC -> epoch ms: 1783311000000
    punch_in_ms = 1783311000000
    res_in = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms},
    )
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["date"] == "2026-07-06"
    assert data_in["late_minutes"] == 0
    assert data_in["work_hours"] is None

    # Duplicate punch-in -> 409
    res_in_dup = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": punch_in_ms + 1000},
    )
    assert res_in_dup.status_code == 409

    # Punch out at 19:10:00 IST (40 mins overtime) -> 2026-07-06 13:40:00 UTC -> epoch ms: 1783345200000
    punch_out_ms = 1783345200000
    res_out = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out_ms},
    )
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["work_hours"] == 9.5
    assert data_out["overtime_minutes"] == 40
    assert data_out["half_day"] is False

    # Already punched out -> 409
    res_out_dup = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": punch_out_ms + 1000},
    )
    assert res_out_dup.status_code == 409


def test_punch_in_late_boundary():
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Late User",
            "email": "late@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # 09:40:01 IST -> 2026-07-06 04:10:01 UTC -> 1783311001000 -> 10 minutes late!
    punch_in_ms = 1783311001000
    res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0002", "punched_at": punch_in_ms},
    )
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10


def test_overnight_shift():
    # 22:00 to 06:00 shift
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0005",
            "name": "Night Shift User",
            "email": "night@example.com",
            "department": "Ops",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01",
        },
    )

    # Punch in 2026-07-06 21:55 IST -> 2026-07-06 16:25:00 UTC -> 1783355100000
    res_in = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": 1783355100000},
    )
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["date"] == "2026-07-06"
    assert data_in["late_minutes"] == 0

    # Punch out 2026-07-07 06:40 IST (40 mins OT) -> 2026-07-07 01:10:00 UTC -> 1783386600000
    res_out = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0005", "punched_at": 1783386600000},
    )
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["work_hours"] == 8.75
    assert data_out["overtime_minutes"] == 40


def test_regularize_attendance():
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Punch in late: 10:05 IST -> 2026-07-06 04:35:00 UTC -> 1783312500000 (35 mins late)
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500000})

    # Regularize punch_in to 09:28 IST -> 2026-07-06 03:58:00 UTC -> 1783310280000
    patch_payload = {
        "punch_in": 1783310280000,
        "reason": "Corrected biometric glitch",
        "regularized_by": "hr.admin",
    }
    res_patch = test_client.patch("/attendance/EMP0001/2026-07-06", json=patch_payload)
    assert res_patch.status_code == 200
    data_patch = res_patch.json()
    assert data_patch["late_minutes"] == 0
    assert len(data_patch["history"]) == 1
    h = data_patch["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "Corrected biometric glitch"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}

    # Request changes nothing -> 422
    res_no_change = test_client.patch("/attendance/EMP0001/2026-07-06", json=patch_payload)
    assert res_no_change.status_code == 422


def test_analytics_and_explain():
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    # Monthly analytics
    res_m = test_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_m.status_code == 200
    data_m = res_m.json()
    assert data_m["emp_code"] == "EMP0001"
    assert data_m["working_days"] == 23

    # Department summary
    res_ds = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res_ds.status_code == 200
    data_ds = res_ds.json()
    assert len(data_ds["items"]) == 1
    assert data_ds["items"][0]["headcount"] == 1

    # Late leaderboard
    res_lb = test_client.get("/analytics/leaderboard/late?month=2026-07")
    assert res_lb.status_code == 200

    # Trend
    res_tr = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res_tr.status_code == 200
    data_tr = res_tr.json()
    assert len(data_tr["items"]) == 7

    # Explain endpoint
    res_exp = test_client.get("/admin/explain/attendance_list?page=1&page_size=10")
    assert res_exp.status_code == 200
    assert res_exp.json()["endpoint"] == "attendance_list"
