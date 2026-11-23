# F1 Watch-Party Companion Discord Bot

A multi-server Discord bot built with `discord.py` and SQLite to coordinate Formula 1 watch parties across private Discord servers while everyone watches on their own F1 TV or broadcast stream.

## Features

- **Per-Server Configuration (`/setup`, `/config`)**: Announcement channel, ping role, watch-party voice category, spoiler delay, session reminder lead time, and VC auto-cleanup timer.
- **Schedules & Discord Scheduled Events (`/next`, `/schedule`, `/events-sync`)**: Dynamic Discord timestamps (`<t:UNIX:F>`) for local-timezone display, automated pre-session pings, and automatic Discord Scheduled Event creation.
- **Watch-Party Lifecycle (`/watchparty start|stop`)**: Automatically creates a temporary Voice Channel and a dedicated Race Discussion Thread, and deletes the VC once empty.
- **Playback Sync Clock (`/sync start|set|now`)**: Countdown sync so everyone hits play together, manual timestamp adjustment, and spoiler-free ephemeral current-timestamp lookup for late joiners.
- **Spoiler-Delayed Live Race Feed & Replay Mode**: Buffers OpenF1 race control flags, penalties, rain/weather shifts, and pit stops for a configurable delay (default `45s`). Includes a **Replay Mode** using free historical OpenF1 sessions (`replay_session_key`) for testing or rewatching races.
- **Post-Race Results & Standings (`/standings drivers|constructors`, `/results`)**: Powered by the Jolpica-F1 API.

## Discord Developer Portal Setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Open the **Bot** tab, click **Reset Token**, and copy your bot token.
   - No privileged gateway intents (`Message Content`, `Server Members`, `Presence`) are required because all commands use Discord Slash Commands and voice-state tracking.
3. Open **OAuth2 -> URL Generator**:
   - **Scopes**: `bot`, `applications.commands`
   - **Bot Permissions**:
     - `Manage Channels` (auto-create & clean up watch-party voice channels)
     - `Manage Events` (create Discord Scheduled Events)
     - `Send Messages` & `Send Messages in Threads`
     - `Create Public Threads`
     - `Embed Links`
     - `Mention Everyone` (optional, for pinging configured roles)
     - `View Channel` & `Connect`
4. Open the generated invite URL to add the bot to your private servers.

## Local Setup & Running

1. Install dependencies using `uv`:
   ```bash
   uv sync
   ```
2. Copy `.env.example` to `.env` and add your `DISCORD_TOKEN` (and optional `OPENF1_USERNAME` / `OPENF1_PASSWORD` for live OpenF1 sessions):
   ```bash
   cp .env.example .env
   ```
3. Run the bot:
   ```bash
   uv run f1-bot
   ```

## Slash Commands

| Command | Description |
| :--- | :--- |
| `/setup` | Configure announcement channel, ping role, VC category, spoiler delay, reminders, and cleanup timers (Admin). |
| `/config` | View current server settings. |
| `/next` | Show the next Grand Prix weekend and all session times in your local timezone. |
| `/schedule [season]` | Show the full F1 calendar for a season. |
| `/events-sync` | Create Discord Scheduled Events for upcoming GP sessions (Manage Events). |
| `/watchparty start [name] [spoiler_delay] [replay_session_key]` | Start a watch party with a temporary VC, discussion thread, and spoiler-delayed feed. |
| `/watchparty stop` | Stop the active watch party, archive the thread, and remove the temporary VC. |
| `/sync start [countdown]` | Run a live countdown and start the shared playback clock. |
| `/sync set <timestamp>` | Set the shared playback clock (`HH:MM:SS` or `MM:SS`). |
| `/sync now` | Privately check the current synced playback timestamp without spoilers. |
| `/standings drivers [season]` | Show Drivers Championship standings. |
| `/standings constructors [season]` | Show Constructors Championship standings. |
| `/results [season] [round]` | Show Grand Prix results (defaults to latest race). |

## Running Tests

```bash
uv run pytest -v
```
