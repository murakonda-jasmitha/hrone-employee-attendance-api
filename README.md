# HROne Employee Attendance & Analytics API

A FastAPI application backed by MongoDB (PyMongo) implementing an Employee Attendance and Analytics backend with full business rule enforcement, atomic concurrency guarantees, and aggregation pipelines.

## Project Overview

This API manages employee details, attendance punch-ins/punch-outs, manual attendance corrections (regularization) with append-only audit history, monthly employee/department analytics, late-comers leaderboards, department attendance trends, and query performance explanations.

### Technology Stack
- Python 3.11+
- FastAPI
- PyMongo / MongoDB 6.0+
- Pydantic v2
- pytest and FastAPI TestClient

---

## Prerequisites & Local Setup

### Prerequisites
- Python 3.11 or newer
- MongoDB 6.0 or newer (or MongoDB Atlas / local Docker instance: `docker run -p 27017:27017 mongo:7`)

### Environment Variables
Configure the following environment variables (or rely on defaults):
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Database name (default: `attendance_db`)

System environment variables take precedence over local `.env` configuration.

---

## Installation & Running

### 1. Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
```

### 2. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 3. Start the API Server
```bash
uvicorn app.main:app --port 8000
```
The API starts on `http://localhost:8000`. Database indexes are created automatically and idempotently on startup.

### 4. Run Tests
```bash
pytest tests/
```

---

## Implemented Endpoints

### System
- `GET /health`: Database readiness probe (returns 200 when MongoDB is reachable, 503 when down).

### Employees
- `POST /employees`: Create employee with `emp_code` uniqueness guarantee (409 on duplicate).
- `GET /employees`: Paginated employee listing sorted by `emp_code` ascending with department filtering.

### Attendance
- `POST /attendance/punch-in`: Record punch-in, enforce 10-minute late grace period, overnight shift date logic, and atomic double-punch prevention.
- `POST /attendance/punch-out`: Record punch-out, compute work hours (half-up rounding), 30-min overtime boundary, and half-day flag.
- `GET /attendance`: Paginated attendance list sorted by date descending, emp_code ascending.
- `PATCH /attendance/{emp_code}/{date}`: Regularize attendance with derived field recomputation and append-only audit history.

### Analytics
- `GET /analytics/employees/{emp_code}/monthly`: Monthly attendance summary per employee.
- `GET /analytics/departments/summary`: Department monthly summary with headcount inclusion for employees with zero logs.
- `GET /analytics/leaderboard/late`: Late-comers leaderboard using standard competition ranking (1, 2, 2, 4) with limit applied after ranking.
- `GET /analytics/departments/{department}/trend`: Daily attendance trend with complete date coverage and 7-day moving averages.

### Admin
- `GET /admin/explain/{endpoint}`: MongoDB execution stats query plan inspection for supported endpoints.
