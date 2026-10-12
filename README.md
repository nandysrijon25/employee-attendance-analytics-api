# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance, punch-in/punch-out, manual corrections with an audit trail, and attendance analytics.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in your environment before starting the app. A local `.env` is supported for development; do not commit credentials. The service expects MongoDB 6.0 or newer.

Open `http://127.0.0.1:8000/docs` to explore the API. `GET /health` checks MongoDB connectivity.

## Notes

The implementation is intended to follow the supplied Contract v2.0, including epoch-millisecond API timestamps, IST attendance dates, MongoDB aggregation-based analytics, and correction history. Run contract and concurrency tests against the target MongoDB version before submission; local happy-path tests do not establish that every edge case passes.
