# BD Govt Job Alert → Telegram

Checks Bangladesh government job circular websites every 2 hours and sends
new circulars to your Telegram. Runs free on GitHub Actions — no server needed.

## Setup (about 10 minutes)

**1. Create your Telegram bot**
- In Telegram, search for **@BotFather** (blue tick) and open it.
- Send `/newbot`, choose a name (e.g. `BD Job Alert`) and a username ending in `bot` (e.g. `myjobalert_bd_bot`).
- BotFather replies with a **token** like `123456789:AAH...`. Copy it and keep it secret.

**2. Get your chat ID**
- Open your new bot in Telegram and press **Start** (or send it any message, like `hi`).
- In a browser, open: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
  (replace `<YOUR_TOKEN>` with your token).
- Find `"chat":{"id":123456789` — that number is your **chat ID**.
  If you see `"result":[]`, send the bot another message and refresh the page.

**3. Put the code on GitHub**
- Create a free account at https://github.com and make a new repository (private is fine).
- Upload all these files, keeping the `.github/workflows/` folder structure.

**4. Add your secrets**
In the repo: Settings → Secrets and variables → Actions → New repository secret
- `TELEGRAM_BOT_TOKEN` = the token from step 1
- `TELEGRAM_CHAT_ID` = the number from step 2

**5. Start it**
Go to the **Actions** tab → enable workflows → "BD Govt Job Alert" → **Run workflow**.
The first run only saves the current circulars (so you aren't flooded with old ones).
After that, you'll get a Telegram message whenever something new appears.

## Send alerts to a group or channel (optional)
- **Group:** add the bot to the group, send a message there, then use `getUpdates` again — group IDs start with `-`.
- **Channel:** add the bot as an admin, and use `@yourchannelname` as the chat ID.

## Customise
- **Add more sites:** edit `SOURCES` in `job_alert.py` (any ministry or department site works).
- **Change how often:** edit the `cron` line in the workflow (`0 */1 * * *` = every hour).
- **Keywords:** edit `KEYWORDS` to match Bangla/English words used on those sites.

## Notes
- GitHub pauses scheduled jobs in repos with no activity for 60 days; if alerts stop, open the Actions tab and re-enable it.
- If a site changes its layout or goes down, that site is skipped and the others still work. Check the Actions log to see which sites are working.
- Test locally: `pip install -r requirements.txt && python job_alert.py` (without keys, it prints messages instead of sending).
