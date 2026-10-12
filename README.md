# Employee Attendance API

An attendance API built with Python, FastAPI and MongoDB. It supports employee records, punch-in and punch-out, attendance corrections with history, and attendance reports. All application code is in `app/main.py`.

## Setup

You need Python 3.11+ and a running MongoDB instance (6.0 or newer).

From the repository folder, create a virtual environment and install the dependencies. On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set your MongoDB connection details:

```powershell
$env:MONGO_URI = 'mongodb://localhost:27017'
$env:MONGO_DB = 'attendance_db'
```

These are also the defaults if you do not set them. You can use a local `.env` file instead; environment variables take priority. Do not upload the `.env` file or credentials to GitHub.

Start the app:

```text
uvicorn app.main:app --port 8000
```

API docs are available at `http://localhost:8000/docs`. Use `GET /health` to check the database connection. The app creates its indexes at startup, so MongoDB needs to be running before you start it.

## How it works

- Attendance dates and shifts use IST. Timestamps are stored in UTC and returned as epoch milliseconds.
- Punch-out calculates work hours, overtime and half-day status.
- Manual corrections keep a history of what changed, who changed it and why.
- Database checks prevent duplicate records and conflicting updates.
- Reports use MongoDB aggregation pipelines. The explain endpoint shows how MongoDB runs those queries.

`REVIEW.md` lists the starter code fixes and contract interpretations. `DECISIONS.md` explains the main design choices. All 12 required operations are implemented.

## Testing

66 tests passed locally using Python 3.12 and MongoDB 8.3. They covered validation, attendance rules, simultaneous requests, corrections and reports.

The app was also tested with 100,000 attendance records. Startup took about 2.13 seconds, and all five explain queries used indexes without collection scans. A fresh installation and a database outage check also passed.

Tests are kept outside this submission folder. MongoDB 6.0/7.0 and the hidden grading dataset have not been tested. Local timings may differ on other machines.

## Nandik Dawar
