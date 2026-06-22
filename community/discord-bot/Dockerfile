# Minimal image for hosting the Zu community Discord bot.
#   docker build -t zu-discord-bot .
#   docker run --rm -e DISCORD_BOT_TOKEN=... zu-discord-bot
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# The bot is a long-running client; no ports to expose.
CMD ["python", "bot.py"]
