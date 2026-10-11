"""
Comprehensive Test Suite verifying Business Rules R1-R10 and All Endpoints
"""
import pytest
from fastapi.testclient import TestClient
import mongomock

import app.main as main_module

mock_client = mongomock.MongoClient()
mock_db = mock_client["test_db"]

main_module.db = mock_db
main_module.client = mock_client

test_client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def setup_db():
    main_module.db.employees.delete_many({})
    main_module.db.attendance_logs.delete_many({})
    main_module.init_db_indexes()
    yield


def test_r1_and_r2_late_grace_boundaries():
    # Employee with shift 09:30 to 18:30
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Test Grace",
            "email": "grace@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # 1. 09:40:00 IST -> 2026-07-06 04:10:00 UTC -> 1783311000000 -> 0 late mins (exactly 10 min grace)
    r1 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783311000000})
    assert r1.status_code == 201
    assert r1.json()["late_minutes"] == 0

    main_module.db.attendance_logs.delete_many({})

    # 2. 09:40:01 IST -> 2026-07-06 04:10:01 UTC -> 1783311001000 -> 10 late mins (strictly > 10 min grace)
    r2 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783311001000})
    assert r2.status_code == 201
    assert r2.json()["late_minutes"] == 10

    main_module.db.attendance_logs.delete_many({})

    # 3. 10:15:59 IST -> 2026-07-06 04:45:59 UTC -> 1783313159000 -> 45 late mins
    r3 = test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783313159000})
    assert r3.status_code == 201
    assert r3.json()["late_minutes"] == 45


def test_r3_overtime_30min_boundary():
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "OT Test",
            "email": "ot@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Punch in 09:30 IST -> 1783310400000
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310400000})

    # Punch out at 18:59:00 IST (29 mins OT) -> 2026-07-06 13:29:00 UTC -> 1783344540000 -> 0 OT mins
    r_29 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783344540000})
    assert r_29.status_code == 200
    assert r_29.json()["overtime_minutes"] == 0

    # Reset punch out to test 30 mins OT
    main_module.db.attendance_logs.update_one({"emp_code": "EMP0001"}, {"$set": {"punch_out": None, "work_hours": None}})

    # Punch out at 19:00:00 IST (30 mins OT) -> 2026-07-06 13:30:00 UTC -> 1783344600000 -> 30 OT mins
    r_30 = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783344600000})
    assert r_30.status_code == 200
    assert r_30.json()["overtime_minutes"] == 30


def test_r4_and_r5_half_day_boundary():
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "HD Test",
            "email": "hd@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Punch in 09:30 IST
    test_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310400000})

    # Worked 4 hours 29 mins 30 secs (4.49 hours -> half_day = True)
    # 09:30 + 4h 29m 30s = 13:59:30 IST -> 2026-07-06 08:29:30 UTC -> 1783326570000
    r_hd = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783326570000})
    assert r_hd.status_code == 200
    assert r_hd.json()["work_hours"] == 4.49
    assert r_hd.json()["half_day"] is True

    # Reset
    main_module.db.attendance_logs.update_one({"emp_code": "EMP0001"}, {"$set": {"punch_out": None, "work_hours": None}})

    # Worked 4 hours 30 mins (4.50 hours -> half_day = False)
    # 14:00:00 IST -> 2026-07-06 08:30:00 UTC -> 1783326600000
    r_full = test_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783326600000})
    assert r_full.status_code == 200
    assert r_full.json()["work_hours"] == 4.5
    assert r_full.json()["half_day"] is False


def test_leaderboard_competition_ranking_ties():
    # 3 employees in Sales
    for code, name in [("EMP0001", "Alice"), ("EMP0002", "Bob"), ("EMP0003", "Charlie"), ("EMP0004", "Dave")]:
        test_client.post(
            "/employees",
            json={
                "emp_code": code,
                "name": name,
                "email": f"{name}@example.com",
                "department": "Sales",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            },
        )

    # Record late minutes:
    # EMP0001: 60 mins late
    # EMP0002: 40 mins late
    # EMP0003: 40 mins late (tied with EMP0002)
    # EMP0004: 20 mins late
    for code, late in [("EMP0001", 60), ("EMP0002", 40), ("EMP0003", 40), ("EMP0004", 20)]:
        main_module.db.attendance_logs.insert_one(
            {
                "emp_code": code,
                "date": "2026-07-06",
                "status": "PRESENT",
                "late_minutes": late,
                "overtime_minutes": 0,
                "half_day": False,
            }
        )

    res = test_client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
    assert res.status_code == 200
    items = res.json()["items"]
    # Ranks should be 1, 2, 2 (EMP0004 with rank 4 is excluded by limit=3)
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0001" and items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP0002" and items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP0003" and items[2]["rank"] == 2


def test_department_summary_no_logs_headcount():
    # EMP0001 has logs, EMP0002 has NO logs at all
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Active User",
            "email": "active@example.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )
    test_client.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Inactive User",
            "email": "inactive@example.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    )

    # Insert 1 log for EMP0001
    main_module.db.attendance_logs.insert_one(
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 0,
            "half_day": False,
        }
    )

    res = test_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "HR"
    assert items[0]["headcount"] == 2  # Includes employee with NO logs!
    assert items[0]["present_days"] == 1.0


def test_explain_all_endpoints():
    endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    for ep in endpoints:
        res = test_client.get(f"/admin/explain/{ep}?emp_code=EMP0001&month=2026-07&department=Engineering&from=2026-07-01&to=2026-07-05")
        assert res.status_code == 200
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
