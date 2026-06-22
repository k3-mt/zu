# Setting up the Zu Discord

A repeatable checklist for standing up (or rebuilding) the community server. The
repo already ships everything that points *at* the server — the README badge and
Community section, the `CONTRIBUTING` link, the GitHub→Discord notify workflow,
and the bot in [`community/discord-bot/`](../../community/discord-bot/). This doc
covers the parts that can only happen inside the Discord app.

> The live invite is **https://discord.gg/zRVHsjKv**, already wired into the README,
> CONTRIBUTING, and this doc. If you ever re-issue it, update every occurrence:
> `grep -rn "discord.gg/" .`

## 1. Create the server

1. Discord → **Add a Server** → **Create My Own** → name it **Zu**.
2. Set the icon (the 🦓 from the README works) and a banner if you have Boost.

## 2. Enable Community mode

**Server Settings → Enable Community.** This unlocks announcement channels,
welcome screen, rules screening, and the public widget. Turn it on first — some
later steps depend on it.

## 3. Channels

A small, legible structure beats a big one. Suggested:

**Welcome**
- `#start-here` (read-only) — what Zu is, links to README + construction guide, the rules.
- `#announcements` (announcement channel) — releases land here via the webhook.

**Community**
- `#general` — chat.
- `#help` — usage questions. Point newcomers here; the bot's `/run` and `/docs` live here.
- `#show-and-tell` — agents people built.

**Development**
- `#contributing` — PRs, design talk; pairs with GitHub Discussions.
- `#capability-gaps` — "Zu couldn't do X." The bot's `/gap` explains how to file these.
- `#github` — issue/PR firehose via the webhook (keep separate from `#announcements`).

## 4. Roles

- `@Maintainer` — manage messages, kick/ban, manage channels.
- `@Contributor` — a thank-you role for merged PRs (cosmetic).
- `@Bot` — for the community bot; Send Messages is enough.
- `@everyone` — view + send in community channels; **no** send in `#start-here` /
  `#announcements`.

## 5. Onboarding & safety

- **Rules screening** (Community → Onboarding): require accepting rules grounded in
  the repo's [Code of Conduct](../../CODE_OF_CONDUCT.md).
- **Verification level**: Medium (verified email) is a good default against spam.
- **Welcome screen**: point new members at `#start-here` and `#help`.

## 6. Wire the GitHub → Discord webhook

Releases and new issues post automatically via
[`.github/workflows/discord-notify.yml`](../../.github/workflows/discord-notify.yml).

1. Discord: **Server Settings → Integrations → Webhooks → New Webhook**. Point it at
   `#announcements` (or `#github`) and **Copy Webhook URL**.
2. GitHub: **Settings → Secrets and variables → Actions → New repository secret**,
   name it `DISCORD_WEBHOOK`, paste the URL.

The workflow no-ops if the secret is missing, so nothing breaks before you set it.
To test: publish a draft release, or open a throwaway issue.

## 7. Add the bot

Follow [`community/discord-bot/README.md`](../../community/discord-bot/README.md) to
create the bot application, invite it (scopes `bot` + `applications.commands`), and
host it. Its `/docs`, `/run`, and `/gap` commands cover the most common questions.

## 8. The invite

The current invite — **https://discord.gg/zRVHsjKv** — is already linked from the
README, CONTRIBUTING, and this doc. Two follow-ups worth doing:

1. Make sure it **never expires** (right-click a channel → Invite People → Edit →
   Expire After: Never, Max Uses: No Limit), so the links in the repo don't go dead.
2. Once the public widget is enabled, optionally switch the README badge to the live
   member-count form noted in its HTML comment. If you have a vanity URL (Boost level
   3), you can also set one under Server Settings → Vanity URL.
