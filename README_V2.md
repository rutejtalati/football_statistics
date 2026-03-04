# V2 Rebuild Export

This folder is a curated export from the current repo for a v2 rebuild.

## Required Environment Variables
- `APIFOOTBALL_API_KEY` (required)
- `APIFOOTBALL_BASE_URL` (optional, provider base URL override)
- `CORS_ORIGINS` (optional, comma-separated origins)
- Frontend variables from `frontend/.env.example` and `frontend/.env.production` as needed

## Render Deployment
- Build command: `pip install -r backend/requirements.txt`
- Start command: `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT`

## Local Run
- Backend:
  - `cd backend`
  - `uvicorn main:app --reload --host 127.0.0.1 --port 8000`
- Frontend:
  - `cd frontend`
  - `npm run dev`
