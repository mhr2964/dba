# DBA Deployment Guide

## Railway (Recommended)

Railway provides managed Postgres and zero-config deploys from GitHub. Free tier includes ~$5 credit/month — sufficient for a bot running at ~50-100 MB RAM.

### Steps

1. Create an account at [railway.app](https://railway.app)
2. New project → **Deploy from GitHub repo** → select this repository
3. In the project, click **+ New** → **Database** → **Add PostgreSQL**
4. Railway automatically sets `DATABASE_URL` in your bot service's environment from the Postgres plugin
5. Add the one remaining secret: in your bot service → **Variables** → add `DISCORD_TOKEN`
6. Railway will build from `Dockerfile` and start `python main.py` (see `railway.json`)
7. On first deploy, open a Railway shell and run migrations:
   ```bash
   python -m alembic upgrade head
   ```
8. The bot is now live 24/7. Railway restarts it on failure (up to 3 retries, then alerts).

### Notes

- `DATABASE_URL` is wired automatically by the Railway Postgres plugin — do not set it manually.
- Logs are available in the Railway dashboard under your service's **Logs** tab.
- To redeploy after a `git push`, Railway triggers automatically if GitHub integration is connected.

---

## Fly.io (Alternative)

Fly.io runs containers globally with their own managed Postgres offering.

### Steps

1. Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
2. From the project root:
   ```bash
   fly launch
   ```
   Accept the detected `Dockerfile`. Use the generated app name or set `app = "dba-bot"` in `fly.toml`.
3. Set the bot token secret:
   ```bash
   fly secrets set DISCORD_TOKEN=your_token_here
   ```
4. Create and attach Postgres:
   ```bash
   fly postgres create
   fly postgres attach --app dba-bot
   ```
   This sets `DATABASE_URL` automatically in your app's environment.
5. Deploy:
   ```bash
   fly deploy
   ```
   The `release_command` in `fly.toml` runs `python -m alembic upgrade head` before each new version goes live.

### Notes

- Migrations run automatically on every `fly deploy` — no manual shell step needed.
- Use `fly logs` to tail live output.
- `fly status` shows current machine state and region.

---

## Database Migrations

Whenever a deploy includes new migration files, run:

```bash
python -m alembic upgrade head
```

On Railway: open the service shell and run the command above.
On Fly.io: migrations run automatically via the `release_command`.

To check current migration state:

```bash
python -m alembic current
```

---

## Updating the Bot

```bash
git pull
# If new migration files are included:
python -m alembic upgrade head
# Then restart the bot process (Railway/Fly redeploy on push)
```

On Railway with GitHub integration connected, a `git push` to the main branch triggers a redeploy automatically.
