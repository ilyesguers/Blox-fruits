# Telegram Dollar Bot

## Deploy on Railway

1. Create a Railway project and deploy this GitHub repository.
2. In Railway **Variables**, add `BOT_TOKEN` with a fresh token from Telegram's `@BotFather`.
3. Attach a Railway Volume mounted at `/data` to preserve the JSON database between deployments.
4. The start command is configured in `railway.json` as `python main.py`.
5. Run only one replica: this bot uses Telegram long polling.
6. After deployment, inspect Railway logs and send `/start` to the bot. The configured admin can open the panel with `/banantak`.

## Security

Do not commit Telegram bot tokens. The previous Python source contained a real token; it has been removed from the deployable source. **Revoke that old token in @BotFather and generate a new one**, then set the new value as Railway's `BOT_TOKEN` variable. Do not paste the token into chat or GitHub.

## Local run

```bash
python -m pip install -r requirements.txt
python main.py
``

Set `BOT_TOKEN` in your environment before running.
