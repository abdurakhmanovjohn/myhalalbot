# Halal Bot

A Telegram bot that delivers accurate, location-based prayer times and helps you build a consistent prayer habit. It sends reminders before and at the time of each prayer, lets you log prayers for any day, and tracks your streaks and progress over time.

## Features

- **Location-based prayer times** — uses the [Aladhan API](https://aladhan.com/prayer-times-api) to compute timings from your shared location and resolves your timezone automatically. Today's timings include sunrise (the end of Fajr time).
- **Two-stage prayer reminders** — a configurable heads-up before each prayer (so you can make Wudu) and an alert at the exact adhan time with a one-tap _Mark as Prayed_ button.
- **Fajr-end reminders** — a warning N minutes before sunrise ("Fajr ends in N minutes, hurry") and an exact notification at sunrise ("Fajr time has ended") so you never accidentally miss the window.
- **Time-gated logging** — logging a prayer on the current day is blocked if the adhan hasn't fired yet; the bot replies with the scheduled adhan time so you know when to come back.
- **End-of-day summary** — an automatic recap at 21:00 your local time, with a weekly recap on Sundays and a monthly recap on the last day of the month.
- **Manual logging** — toggle any of the five daily prayers on or off. Navigate day by day, jump back to _Today_, or open a month **calendar** to pick any past date. Future dates are locked.
- **Streaks** — current and longest streak of fully-completed days, shown in your profile. An unfinished today never breaks your streak.
- **Reports** — on-demand daily, 7-day, and 30-day summaries with completion rate, perfect-day count, a per-prayer breakdown, and your most-missed prayer(s). Stats are scaled to how long you've actually been tracking, so new users aren't penalised for days before they joined, and ties for the most-missed prayer are all listed.
- **30-day overview** — a GitHub-style activity grid colouring each day by how many prayers you completed. Days before you started show as outside the tracked range rather than as misses.
- **Settings** — change your reminder lead time (5/10/15/20/30 min, default 15) and your Asr calculation method (Hanafi or Standard/Shafi'i); changes apply immediately. A **Danger Zone** section lets you reset all prayer logs (keeping your location and settings) or delete your entire account and stop all reminders.
- **Persistent menu** — a reply keyboard stays visible in the chat for one-tap access to all main functions without needing to type commands.
- **Change location** — update your timezone and prayer times at any time without losing your settings or logs.

## Tech stack

- **Python** with [aiogram 3](https://docs.aiogram.dev/) for the Telegram layer
- **PostgreSQL** via [asyncpg](https://magicstack.github.io/asyncpg/) (connection pooling)
- **APScheduler** for queuing per-user reminders and summaries
- **aiohttp** for the Aladhan API calls
- **pytz** for per-user timezone handling

## Project structure

```
halal_bot/
├── main.py          # Bot entry point: handlers, keyboards, scheduling
├── prayer_api.py    # Aladhan API client (timings + timezone)
├── db.py            # Creates / migrates the database schema
├── requirements.txt
├── .env             # Secrets and DB credentials (not committed)
└── README.md
```

## Database schema

**users**

| Column               | Type    | Notes                            |
| -------------------- | ------- | -------------------------------- |
| user_id              | BIGINT  | Telegram user id (primary key)   |
| username             | VARCHAR |                                  |
| full_name            | VARCHAR |                                  |
| timezone             | VARCHAR | Resolved from location           |
| latitude / longitude | DOUBLE  | Used to recompute daily timings  |
| reminder_offset_mins | INTEGER | Heads-up lead time, default 15   |
| asr_school           | INTEGER | 1 = Hanafi, 0 = Standard/Shafi'i |

**prayer_logs** (one row per prayer per day)

| Column       | Type    | Notes                                         |
| ------------ | ------- | --------------------------------------------- |
| log_id       | SERIAL  | Primary key                                   |
| user_id      | BIGINT  | References `users(user_id)` on delete cascade |
| prayer_name  | VARCHAR | One of Fajr, Dhuhr, Asr, Maghrib, Isha        |
| prayer_date  | DATE    |                                               |
| is_completed | BOOLEAN |                                               |
|              |         | `UNIQUE(user_id, prayer_name, prayer_date)`   |

## Setup

1. **Clone and enter the project**

   ```bash
   git clone https://github.com/abdurakhmanovjohn/myhalalbot.git
   cd myhalalbot
   ```

2. **Create a virtual environment and install dependencies**

   ```bash
   python3 -m venv venv
   source venv/bin/activate        # on Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Create a `.env` file** in the project root:

   ```env
   BOT_TOKEN=your_telegram_bot_token
   DB_USER=your_db_user
   DB_PASSWORD=your_db_password
   DB_NAME=your_db_name
   DB_HOST=localhost
   DB_PORT=5432
   ```

   Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).

4. **Create the database tables**

   ```bash
   python db.py
   ```

   This is safe to re-run; it creates tables if missing and adds the `asr_school`
   column to an existing `users` table.

5. **Run the bot**

   ```bash
   python main.py
   ```

## Commands

All commands are also accessible via the persistent reply keyboard.

| Command      | Keyboard button    | Description                         |
| ------------ | ------------------ | ----------------------------------- |
| `/start`     | —                  | Set location and set up reminders   |
| `/log`       | Log Prayers        | Log prayers (with calendar & today) |
| `/schedule`  | View Schedule      | Today's upcoming alerts             |
| `/report`    | Reports            | Daily / weekly / monthly reports    |
| `/overview`  | 30-Day Overview    | 30-day activity grid                |
| `/profile`   | Profile            | View stats and streaks              |
| `/settings`  | Settings           | Reminder lead time & Asr method     |
| `/location`  | Change Location    | Update your location                |
| `/help`      | —                  | How the bot works                   |

## Notes

- **Time format.** Times are displayed in 24-hour format throughout, controlled by a single `TIME_FMT` constant in `main.py` (set it to `"%I:%M %p"` for 12-hour AM/PM).
- **Calculation method.** Timings use Aladhan `method=14` by default, with the Asr school selectable per user (Hanafi by default). The method itself isn't yet user-configurable; it can be exposed in Settings if needed, since the method dramatically affects Fajr and Isha times across regions.
- **Scheduling persistence.** Reminders and summaries are held in APScheduler's in-memory store. They are re-queued automatically at 00:01 local each day and again on startup, so the bot should run as a long-lived process for reminders to fire reliably.
- **Security.** Keep `.env` out of version control (it's listed in `.gitignore`). If a bot token is ever exposed, revoke and reissue it via @BotFather.