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
`seen.json` and the `debug/` folder are created by the bot.

## Sites
| Site | Status | What is read |
|---|---|---|
| Teletalk All Jobs | ✅ | Every organisation, its posts (with links) and publish date |
| Bangladesh Army | ✅ | News & Updates (date + title) and Current Circulars |
| BPSC | 🛠 not yet | Its circular lists are on sub-pages; manual runs save them in `debug/` |

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
- **From date**: e.g. `01-11-2026` (empty = today)
- **To date**: e.g. `12-11-2026` (empty = today)

So for "the last 15 days" type only the From date and leave To empty.

You get:
1. "🔎 Checking job sites…" right away (shows Telegram works)
2. One message per circular published in that range, with posts and their links
3. "📊 Teletalk daily update"
4. "🤖 Manual check finished" with a status line for every site

At most 60 circulars are sent per run; the report says so if there were more.

## Automatic runs
Every 2 hours, only circulars that are new since the last run are sent.
The very first run only saves what is already there (no flood of old circulars).

## If something goes wrong
- Run is red with "secret is missing" → check the secret names in step 3.
- Run is red with "Telegram message failed" → wrong token/chat id, or you didn't press Start in the bot.
- Report shows ❌ → run it manually once more. Manual runs save the pages the bot
  downloaded in the `debug/` folder of this repo; those files show what went wrong.

## Customise
- How often: edit the `cron` line in the workflow.
- New site: each site has its own reader in `job_alert.py` (`read_teletalk`, `read_army`),
  written from that site's real page. A new site needs its own reader.
- GitHub pauses schedules after 60 days without repo activity; re-enable in the Actions tab.
