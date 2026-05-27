# 📋 Planning Bot

A Telegram bot for personal task management with natural language input and Google Calendar integration.

## Features

- **Natural language** — just type normally: `finish report by next Friday`
- **Two-layer storage** — tasks live in SQLite; only scheduled ones go to Google Calendar
- **Full CRUD** — add, list, update, complete, delete tasks
- **Smart scheduling** — list tasks, pick numbers, block time on your calendar
- **Single-user** — secured by your Telegram user ID

---

## Quick Start

### 1. Clone and install

```bash
git clone <your-repo>
cd telegram-planner
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create your Telegram bot

1. Open Telegram → search `@BotFather`
2. Send `/newbot` and follow the prompts
3. Copy the token — you'll need it in `.env`
4. Get your user ID from `@userinfobot`

### 3. Set up Google Calendar API

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → **APIs & Services** → **Enable APIs**
3. Enable **Google Calendar API**
4. Go to **Credentials** → **Create Credentials** → **OAuth 2.0 Client ID**
5. Application type: **Desktop app**
6. Download the JSON → rename it `credentials.json` → place in project root

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

| Variable             | Description                                      |
|----------------------|--------------------------------------------------|
| `TELEGRAM_TOKEN`     | Token from @BotFather                            |
| `ALLOWED_USER_ID`    | Your Telegram user ID (from @userinfobot)        |
| `GOOGLE_TOKEN_B64`   | Base64 token (generated in step 5)               |
| `GOOGLE_CALENDAR_ID` | `primary` or a specific calendar ID             |
| `TIMEZONE`           | Your timezone (e.g. `Asia/Seoul`, `Europe/London`)|

### 5. Authenticate Google Calendar (run once, locally)

```bash
python auth_calendar.py
```

This opens your browser for consent, writes `token.json`, and prints the base64 value to paste into `GOOGLE_TOKEN_B64`.

### 6. Run locally

```bash
python bot.py
```

---

## Deploy to Railway

### First time

1. Push your code to a GitHub repo (token.json and credentials.json are gitignored — that's fine)
2. Go to [Railway](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Select your repo

### Add environment variables

In Railway → your service → **Variables**, add:

```
TELEGRAM_TOKEN        = <from BotFather>
ALLOWED_USER_ID       = <your Telegram user ID>
GOOGLE_TOKEN_B64      = <output from auth_calendar.py>
GOOGLE_CALENDAR_ID    = primary
TIMEZONE              = Asia/Seoul
DB_PATH               = /data/planner.db
```

### Persistent storage for SQLite

1. In Railway → your service → **Volumes** → **Add Volume**
2. Mount path: `/data`
3. This keeps your `planner.db` alive across deploys

### Deploy

Railway auto-deploys on every push to your repo. The `Procfile` tells it to run `python bot.py`.

---

## Usage

### Natural Language

| Message | Action |
|---------|--------|
| `finish report by next Friday` | Add task with deadline |
| `review PRs by Wednesday 3pm` | Add task with deadline |
| `call dentist` | Add task, no deadline |
| `what do I have this week?` | List this week's tasks |
| `what tasks do I have today?` | List today's tasks |
| `show all tasks` | List all pending tasks |
| `schedule 1 2 3` | Schedule tasks 1, 2, 3 from last list |
| `mark task 1 done` | Complete task |
| `done 2 3` | Complete tasks 2 and 3 |
| `delete task 2` | Delete task (with confirmation) |
| `update task 1 deadline to Monday` | Change deadline |
| `move task 3 to next Thursday` | Reschedule |

### Commands

| Command | Description |
|---------|-------------|
| `/tasks` | All pending tasks |
| `/today` | Tasks due today |
| `/week` | Tasks due this week |
| `/calendar` | Upcoming Google Calendar events |
| `/cancel` | Cancel current operation |
| `/help` | Usage examples |

### Scheduling Flow

```
You:  what do I have this week?
Bot:  📆 This Week's Tasks (3)
      1. ⏳ Finish report — due Fri May 29
      2. ⏳ Review PRs — due Wed May 27
      3. ⏳ Team sync prep — due Thu May 28

You:  schedule 1 3
Bot:  ⏰ Schedule 1/2
      Task: Finish report
      When? (e.g. Thursday 2pm for 2 hours)

You:  Thursday 2pm for 2 hours
Bot:  ✅ Scheduled! Finish report — Thu May 29, 2:00 PM → 4:00 PM

Bot:  ⏰ Schedule 2/2
      Task: Team sync prep
      When?

You:  Friday 10am for 30 minutes
Bot:  ✅ Scheduled! Team sync prep — Fri May 30, 10:00 AM → 10:30 AM
Bot:  🎉 All tasks scheduled!
```

---

## Project Structure

```
telegram-planner/
├── bot.py              # Telegram handlers + state machine
├── nlp.py              # Intent detection + datetime/title extraction
├── db.py               # SQLite CRUD
├── calendar_client.py  # Google Calendar API wrapper
├── config.py           # Environment variable loading
├── auth_calendar.py    # One-time local OAuth script
├── requirements.txt
├── Procfile            # Railway process definition
├── .env.example
└── .gitignore
```
