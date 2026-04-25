# BattleX Deployment Guide

This project is now configured for Render deployment and GitHub hosting.

## What is configured

- `requirements.txt` for Python dependencies
- `runtime.txt` to pin Python version for Render
- `Procfile` for a production web command
- `render.yaml` Blueprint config for one-click Render deployment
- `.env.example` for required environment variables
- `.gitignore` to avoid pushing local secrets and local DB files
- `app.py` updated to:
  - use env-based `SECRET_KEY`, DB path, and cookie security
  - support PostgreSQL via `DATABASE_URL` (with SQLite fallback)
  - run correctly behind Gunicorn
  - initialize DB on app import (Render/Gunicorn)
  - support persisted uploads via `/uploads/<filename>` route
  - preserve existing table/schema setup for both SQLite and PostgreSQL

## Deploy on Render

1. Push this repository to GitHub.
2. In Render, choose **New +** -> **Blueprint**.
3. Select this repository (Render will read `render.yaml`).
4. Render will provision both:
  - `battlex-db` (PostgreSQL)
  - `battlex` (web service)
5. Confirm service settings and deploy.
6. On first boot, `init_db()` creates all required tables automatically.
7. Set missing secrets in Render dashboard:
   - `CASHFREE_APP_ID`
   - `CASHFREE_SECRET_KEY`
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GOOGLE_REDIRECT_URI` (use your Render URL callback endpoint)

Notes:
- PostgreSQL is preferred on Render and is auto-wired using `DATABASE_URL`.
- SQLite fallback is still supported (for local/dev) at `/var/data/gamezone.db`.
- Uploaded files are configured to persist at `/var/data/uploads`.
- If you use Google OAuth, add your Render callback URL in Google Console.

## Schema and Tables Location

- Schema bootstrap function: `app.py` -> `init_db()`
- Tables created in code: `users`, `registrations`, `team_members`, `notifications`, `events`
- Runtime DB selection:
  - PostgreSQL when `DATABASE_URL` is set
  - SQLite otherwise

## Upload to GitHub

Run these commands in project root:

```bash
git init
git add .
git commit -m "Prepare BattleX for Render deployment"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

If a git repository already exists, skip `git init` and only set/update `origin` then push.

## Local run

```bash
pip install -r requirements.txt
python app.py
```

App runs on `http://127.0.0.1:5000` by default.
