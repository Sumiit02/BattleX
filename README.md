# BATTLE-X Deployment Guide

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
  - `CASHFREE_WEBHOOK_SECRET`
  - `CASHFREE_WEBHOOK_REQUIRE_SIGNATURE=1`
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
git commit -m "Prepare BATTLE-X for Render deployment"
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

## Cashfree Order API (Frontend)

Use this endpoint to create a Cashfree order and get a `payment_session_id`:

- Method: `POST`
- Path: `/api/cashfree/create-order`
- Content-Type: `application/json`
- Optional Header: `X-Idempotency-Key: <unique-key-per-checkout>`

Example request:

```json
{
  "registration_id": 123,
  "event_id": 7,
  "mode": "BR Solo",
  "email": "player@example.com"
}
```

Example success response:

```json
{
  "success": true,
  "gateway": "cashfree",
  "environment": "sandbox",
  "order_id": "BXR1231714200000",
  "cashfree_order_id": "BXR1231714200000",
  "amount": 10000,
  "currency": "INR",
  "payment_session_id": "session_xxx",
  "payment_link": null,
  "return_url": "http://localhost/payment/cashfree/callback?registration_id=123&order_id={order_id}"
}
```

Note: `/create_payment_order` still works for backward compatibility, but new integrations should use `/api/cashfree/create-order`.

### Idempotency and Retry Behavior

- If the same `X-Idempotency-Key` is retried, the API returns the same active Cashfree `order_id` instead of creating a duplicate order.
- For registration retries, the backend also reuses an existing active `order_id` linked to that registration.

## Cashfree Webhook (Server-to-Server)

Use this endpoint for asynchronous payment confirmation from Cashfree:

- Method: `POST`
- Path: `/api/cashfree/webhook`

Behavior:

- Verifies webhook signature when enabled.
- Re-checks order status directly from Cashfree before finalizing.
- Finalization is idempotent, so repeated webhook deliveries do not double-credit wallet or double-complete registrations.

Recommended env vars:

- `CASHFREE_WEBHOOK_SECRET` (if not set, app falls back to `CASHFREE_SECRET_KEY`)
- `CASHFREE_WEBHOOK_REQUIRE_SIGNATURE=1` (recommended for production)

## Cashfree Payouts (Automatic Bank/UPI Withdrawals)

Withdrawal requests are now wired to a payout provider path when enabled. This is separate from the payment gateway keys.

Required env vars for automatic payouts:

- `CASHFREE_PAYOUTS_ENABLED=1`
- `CASHFREE_PAYOUTS_BASE_URL` (your Cashfree Payouts API base URL)
- `CASHFREE_PAYOUTS_CLIENT_ID`
- `CASHFREE_PAYOUTS_SECRET_KEY`
- `CASHFREE_PAYOUTS_REQUEST_PATH=/requestTransfer`
- `CASHFREE_PAYOUTS_STATUS_PATH=/getTransferStatus`
- `CASHFREE_PAYOUTS_WEBHOOK_SECRET`
- `CASHFREE_PAYOUTS_REQUIRE_SIGNATURE=1`
- `CASHFREE_PAYOUTS_SOURCE=battlex`

Behavior:

- When an admin clicks **Send Payout**, the app attempts a live transfer request.
- If the payout request is accepted, the withdrawal moves to `processing` and a provider reference is stored.
- If the payout request fails, the wallet is refunded automatically and the request is marked `failed`.
- Cashfree payout webhooks can mark the transfer as `paid` or `failed` later using `/api/cashfree/payouts/webhook`.

Render setup note:

- The production blueprint in `render.yaml` enables `CASHFREE_PAYOUTS_ENABLED=1`.
- You still need to paste the payout client id, secret key, and webhook secret into Render before live payouts can actually move money.
