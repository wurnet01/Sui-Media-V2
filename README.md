# HDM — Social Platform

HDM is a Flask social-network application with real user registration, posts, media uploads, reactions, comments and replies, 24-hour stories, friend requests, profiles, notifications, saved posts, blocking/reporting, and direct messages.

## Status

This repository contains a deployment foundation and ongoing functional hardening. The application has not been independently browser-tested against a live production deployment in this workspace. Treat the first deployment as a staging release and complete the checklist below before inviting real users.

## Requirements

- Python 3.11
- PostgreSQL for production (SQLite is used by default for local development)
- A persistent writable disk for uploaded media
- A long random `SECRET_KEY`

## Run locally (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
$env:FLASK_DEBUG = "1"
$env:SECRET_KEY = "local-only-change-me"
python app.py
```

Open http://127.0.0.1:5000 and create your own account. No demo/fake accounts are seeded.

For a local run, the default database is `instance/social_platform.db` (Flask-SQLAlchemy relative SQLite path). Keep local database files and uploaded user media out of Git.

## Run tests

```powershell
pytest -q
```

The current test suite is a starting point, not a complete end-to-end or security certification. Expand it to cover every route and authorization boundary before production launch.

## Deploy on Render

1. Push the `feature/hdm-functional-overhaul` branch to GitHub.
2. In Render, choose **New → Blueprint** and select this repository/branch.
3. Review the PostgreSQL service and persistent upload disk in `render.yaml`. These are paid Render resources; confirm current pricing before creating them.
4. Render generates `SECRET_KEY` and injects the managed PostgreSQL connection string.
5. Set `SOCKETIO_ALLOWED_ORIGINS` to the exact public HTTPS origin if you want to restrict Socket.IO further.
6. Deploy as a staging service first. Verify `/healthz`, account creation, login/logout, upload, story expiry, comments/replies, messaging, and mobile navigation.
7. Configure backups for PostgreSQL and a backup policy for the uploads disk before production use.

### Deployment notes

- Do not deploy with the development secret key, debug mode, or SQLite on an ephemeral filesystem.
- The included Render setup uses one Gunicorn Eventlet worker because the current Socket.IO rooms are process-local. Do not scale to multiple workers/instances until a shared Socket.IO message queue (for example Redis) and a compatible deployment topology are configured.
- Uploaded files are stored on the persistent disk mounted at `static/uploads`. If you move to multiple web instances, migrate media to object storage (S3-compatible) first.
- SQLite is suitable for local development only; production should use PostgreSQL.
- The app uses server-rendered HTML and Bootstrap CDN assets. For strict availability, self-host or pin and monitor third-party frontend assets.

## Environment variables

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Required long random signing key |
| `DATABASE_URL` | SQLAlchemy database URL; PostgreSQL in production |
| `FLASK_DEBUG` | Set to `0` in production |
| `COOKIE_SECURE` | Set to `true` behind HTTPS |
| `SOCKETIO_ALLOWED_ORIGINS` | Optional comma-separated Socket.IO origins |
| `ADMIN_USERNAME` | Optional username permitted to access Flask-Admin |
| `PORT` | HTTP port supplied by hosting provider |

## Production launch checklist

- [ ] Run the test suite on the deployment branch and resolve all failures.
- [ ] Test in a real browser on desktop and mobile.
- [ ] Verify upload file type/content validation and file size limits.
- [ ] Add CSRF protection to every state-changing form before public launch.
- [ ] Add rate limiting for login, registration, comments, friend requests, reports, and messages.
- [ ] Review privacy, retention, moderation, account deletion, and terms/policy requirements.
- [ ] Verify PostgreSQL backups and uploaded-media backups/restores.
- [ ] Configure error monitoring, structured logs, and uptime alerts.
- [ ] Test concurrent Socket.IO connections and reconnect behaviour.
- [ ] Verify all third-party assets and external link previews comply with your security policy.

## Data safety

Database initialization uses `create_all` for missing tables and does not intentionally drop existing tables. Use Flask-Migrate for schema evolution. Always back up the database before applying migrations.
