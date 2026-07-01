# Minimal image for hosting the Zu community Discord bot.
#   docker build -t zu-discord-bot .
#   docker run --rm -e DISCORD_BOT_TOKEN=... zu-discord-bot
FROM python:3.12-slim

# Run as an unprivileged user, not root: the bot needs no root capabilities, so a
# container/image compromise shouldn't hand out uid 0. Create the user up front and
# own /app with it.
RUN useradd --create-home --uid 10001 zeke
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py _config.py ./
RUN chown -R zeke:zeke /app

USER zeke

# The bot is a long-running client; no ports to expose.
CMD ["python", "bot.py"]
