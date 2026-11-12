# BD Govt Job Alert → Telegram

Checks Bangladesh government job circular websites every 2 hours and sends
new circulars to your Telegram. Runs free on GitHub Actions.

## Files
```
job_alert.py
requirements.txt
README.md
.github/workflows/job_alert.yml
```
Do not upload `seen.json` — the bot creates it.

## Setup
1. Make a Telegram bot with **@BotFather** (`/newbot`) and copy the token.
2. Open your bot, press **Start**, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the `"chat":{"id":...}` number.
3. In the GitHub repo: Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN` = token
   - `TELEGRAM_CHAT_ID` = chat id
4. Actions tab → enable workflows.

## Manual run (choose dates)
Actions → **BD Govt Job Alert** → **Run workflow**
- **From date**: e.g. `05-11-2026` (empty = today)
- **To date**: e.g. `12-11-2026` (empty = same as From date)

You get:
1. "🔎 Checking job sites…" right away (shows Telegram works)
2. One message per circular published in that range, with posts and their links
3. "🤖 Manual check finished" with a status line for every site

## Automatic runs
Every 2 hours, only circulars that are new since the last run are sent.
The very first run only saves what is already there (no flood of old circulars).

## If something goes wrong
- Run is red with "secret is missing" → check the secret names in step 3.
- Run is red with "Telegram message failed" → wrong token/chat id, or you didn't press Start in the bot.
- Report shows ❌ or "0 links" → open the run, download **Artifacts → debug-pages**,
  it contains the pages the bot downloaded.

## Customise
- Sites: edit `SOURCES` in `job_alert.py`.
- How often: edit the `cron` line in the workflow.
- GitHub pauses schedules after 60 days without repo activity; re-enable in the Actions tab.
