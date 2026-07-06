# Deployment Guide — Render + GitHub

Follow these steps exactly. Run every command from inside the `telegram-bot/` directory.

---

## 1 — Create a GitHub Repository

```bash
# From the project root (telegram-bot/)
git init
git add .
git commit -m "Initial commit — Telegram Advanced Scheduler Bot"

# Create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git branch -M main
git push -u origin main
```

---

## 2 — Create a MongoDB Atlas Database (free tier)

1. Go to https://cloud.mongodb.com and sign up / log in.
2. Create a **free M0 cluster** (any region).
3. Under **Database Access** → Add a user with Read/Write access.
4. Under **Network Access** → Add `0.0.0.0/0` (allow all IPs — required for Render).
5. Click **Connect** → **Drivers** → copy the connection string:
   ```
   mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/scheduler_bot?retryWrites=true&w=majority
   ```
   Save this as your `MONGO_URI`.

---

## 3 — Create a Telegram Bot

1. Open Telegram and message **@BotFather**.
2. Send `/newbot` and follow the prompts.
3. Copy the **Bot Token** — this is your `TELEGRAM_BOT_TOKEN`.
4. Optional: send `/setprivacy` → Disable (so the bot can read group messages).

---

## 4 — Deploy to Render

### 4a — Create a new Web Service

1. Go to https://render.com and sign in.
2. Click **New +** → **Web Service**.
3. Connect your GitHub account and select your repo.

### 4b — Configure the service

| Setting | Value |
|---|---|
| **Name** | `telegram-scheduler-bot` |
| **Environment** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python main.py` |
| **Instance Type** | Free (or Starter for always-on) |

### 4c — Set Environment Variables

Under **Environment** → **Add Environment Variable**:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `MONGO_URI` | Your MongoDB Atlas connection string |
| `PORT` | `8080` |
| `ADMIN_IDS` | Comma-separated Telegram user IDs, e.g. `12345678,87654321` |

### 4d — Deploy

Click **Create Web Service**. Render will:
1. Pull your code from GitHub
2. Run `pip install -r requirements.txt`
3. Start `python main.py`

Watch the logs — you should see:
```
Configuration loaded successfully.
Starting Telegram Advanced Scheduler Bot…
Keep-alive server thread started.
Connected to MongoDB — database: 'scheduler_bot'
APScheduler started.
Bot starting — polling for updates…
```

---

## 5 — Set Up UptimeRobot (keep Render free tier alive)

Render's free tier spins down after 15 minutes of inactivity.
UptimeRobot pings your service every 5 minutes to prevent this.

1. Sign up at https://uptimerobot.com (free).
2. Click **Add New Monitor**:
   - **Monitor Type**: HTTP(s)
   - **Friendly Name**: Scheduler Bot
   - **URL**: `https://YOUR-SERVICE.onrender.com/health`
   - **Monitoring Interval**: 5 minutes
3. Click **Create Monitor**.

Your bot's keep-alive server responds to `GET /health` with `{"status": "ok"}`.

---

## 6 — Verify the Bot is Running

1. Open Telegram and start a chat with your bot.
2. Send `/start` — you should see the welcome message with inline buttons.
3. Check the Render logs for `"Scheduler tick"` every 60 seconds.

---

## 7 — Pushing Updates

```bash
# After making changes:
git add .
git commit -m "Your update description"
git push origin main
# Render auto-deploys on every push to main.
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `TELEGRAM_BOT_TOKEN is not set` | Check Render env vars — token must not have quotes |
| `MongoDB connection failed` | Verify Atlas network access allows `0.0.0.0/0` |
| Bot not responding | Check Render logs; ensure service is not sleeping (use UptimeRobot) |
| `Forbidden` errors in logs | Bot was removed from the channel — re-add it as admin |
| Posts not firing | Check that `next_run_at` is in UTC and scheduler is running (see logs) |
