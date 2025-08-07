# Simple Telegram Bot

A simple Telegram bot that always replies with "hello" to any text message.

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Get a bot token:**
   - Message [@BotFather](https://t.me/botfather) on Telegram
   - Send `/newbot` and follow the instructions
   - Copy the token you receive

3. **Create environment file:**
   - Create a `.env` file in the project root
   - Add your bot token:
     ```
     TELEGRAM_BOT_TOKEN=your_bot_token_here
     ```

## Run the bot

```bash
python bot.py
```

The bot will start and reply "hello" to any text message it receives.

## How it works

- The bot listens for all text messages (excluding commands)
- It automatically replies with "hello" to any message
- Uses python-telegram-bot library for Telegram API integration
