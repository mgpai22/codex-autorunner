# Discord Bot Runbook

## Purpose

Operate and troubleshoot the Discord gateway bot that proxies Codex app-server
sessions.

## Prerequisites

- A Discord bot application created at https://discord.com/developers/applications
- Privileged intents enabled in the Developer Portal:
  - **Server Members Intent** (for role-based access control)
  - **Message Content Intent** (for reading message text)
- Bot invited to your server with scopes: `bot`, `applications.commands`
- Set env vars in the bot environment:
  - `CAR_DISCORD_BOT_TOKEN`
  - `CAR_DISCORD_GUILD_IDS` (optional, comma-separated guild IDs)
  - `OPENAI_API_KEY` (or the Codex-required key)
  - `CAR_DISCORD_APP_SERVER_COMMAND` (optional full command override)
- Configure `discord_bot` in `codex-autorunner.yml` or `.codex-autorunner/config.yml`
- Ensure `discord_bot.allowed_guild_ids` includes your server's guild ID
- In shared servers, use `discord_bot.trigger_mode: mentions` (default) to avoid
  reacting to every message

Example minimal config:

```yaml
discord_bot:
  enabled: true
  allowed_guild_ids:
    - 123456789012345678
  trigger_mode: mentions
  defaults:
    approval_mode: safe
```

## Start

- `car discord start --path <hub_root>`
- On startup, the bot logs `discord.bot.starting` with config details.
- On ready, the bot logs `discord.bot.ready` with the bot username and guild count.
- Slash commands are synced to each allowed guild on ready.

## Verify

- In a channel where the bot is present, use `/status` and confirm the response.
- Use `/health` to run diagnostics (token validity, state file, gateway status).
- Use `/bind /path/to/workspace` to bind the channel to a workspace.
- Send a message mentioning the bot (`@BotName do something`) and verify a response.

## Common Commands

| Command | Description |
|---------|-------------|
| `/run <prompt> [model] [effort]` | Start an agent task (model default: `gpt-5.3-codex`, effort default: `medium`) |
| `/stop` | Interrupt the active task |
| `/new` | Start a new conversation |
| `/resume [thread_id]` | Resume a previous conversation |
| `/bind <path>` | Bind channel to a workspace |
| `/repos` | List available repositories |
| `/status` | Show current configuration |
| `/model [name]` | Show or change the model |
| `/effort [level]` | Show or change the reasoning effort (thinking level) |
| `/agent [name]` | Show or change the agent |
| `/approvals [mode]` | Set approval mode (safe/yolo) |
| `/health` | Run health diagnostics |
| `/workspace create` | Create a new workspace (interactive flow) |
| `/workspace clone <url> [name]` | Clone a git repo into a new workspace |
| `/workspaces` | List scaffolded workspaces |

## Health Check

Run diagnostics from the CLI without starting the full bot:

```bash
car discord health --path <hub_root>
```

This validates:
- Bot token is set and valid
- State file exists and schema is current
- Gateway connectivity (optional, skipped with `--skip-gateway`)

## State Check

Verify the SQLite schema without starting the bot:

```bash
car discord state-check --path <hub_root>
```

This opens the state database and applies any pending migrations.

## Logs

- Primary log file: `config.log.path` (default `.codex-autorunner/codex-autorunner-hub.log`)
- Discord events are logged as JSON lines with `event` fields such as:
  - `discord.bot.starting` - bot startup with config summary
  - `discord.bot.ready` - gateway connected, guilds loaded
  - `discord.commands.synced` - slash commands synced to guild
  - `discord.run.started` - agent turn started
  - `discord.turn.completed` - agent turn finished
  - `discord.allowlist.denied` - event rejected by allowlist
  - `discord.turn.interrupt_failed` - interrupt attempt failed
  - `discord.lock.acquired` / `discord.lock.contended` - instance lock events
- App-server events are logged with `app_server.*` events.

## Troubleshooting

### Bot not responding to messages

1. Confirm `CAR_DISCORD_BOT_TOKEN` is set and valid
2. Confirm `discord_bot.allowed_guild_ids` includes the server's guild ID
3. Confirm the channel is in `allowed_channel_ids` (or leave empty for all channels)
4. Check `discord_bot.trigger_mode`:
   - If `mentions` (default): you must @mention the bot or reply to its message
   - If `all`: the bot responds to every message in allowed channels
5. Check for `discord.allowlist.denied` events in the log

### Slash commands not appearing

1. Commands sync to guilds listed in `allowed_guild_ids` on `on_ready()`
2. Check for `discord.commands.sync_failed` events in the log
3. Ensure the bot was invited with the `applications.commands` scope
4. Try removing and re-inviting the bot, or wait up to 1 hour for global sync

### Interaction failed / "This interaction failed"

1. All handlers must `defer()` within 3 seconds; check for slow startup code
2. Check for exceptions in the handler logged as `discord.commands.*` events
3. Ensure the bot has `Send Messages` and `Use Application Commands` permissions
   in the channel

### Approval buttons not working after restart

1. PersistentViews are re-registered in `on_ready()`
2. Check `discord_pending_approvals` table in the state database
3. Stale approvals (past TTL) are cleaned up on startup

### Turns failing

1. Check `discord.turn.failed` and `app_server.*` logs
2. Verify the Codex app-server is installed and the command is correct:
   - `discord_bot.app_server_command` in config, or
   - `CAR_DISCORD_APP_SERVER_COMMAND` env var
3. Check `discord_bot.app_server.turn_timeout_seconds` if turns are timing out

### Instance lock contention

- Error: "Discord bot already running for this token"
- Check if another instance is running (the lock file contains PID and hostname)
- Lock file location: `~/.codex-autorunner/locks/discord_<sha256_prefix>.lock`
- If the previous process crashed, the stale lock is auto-cleaned on next start
  (only if the PID is no longer alive)

### Message not delivered / outbox issues

1. Check `discord_outbox` table in the state database for stuck records
2. Outbox retries: 0.5s, 2s, 5s (immediate), then 10s intervals, max 8 attempts
3. After 8 failures the record is abandoned
4. Rate limit errors from Discord include `retry-after` which the outbox respects

### Bot privileges / permissions

Ensure the bot has these permissions in the channels it operates in:

- Send Messages
- Send Messages in Threads
- Embed Links
- Attach Files
- Read Message History
- Use Application Commands
- Create Public Threads (if creating threads for tasks)

### Progress embeds not updating

1. Check `discord_bot.progress_stream.enabled` is `true` (default)
2. Edit rate limit: Discord allows ~5 edits per 5s per channel
3. The bot uses a 1.5s minimum interval between edits
4. Check for rate limit warnings in the log

## Stop

- Stop the process with Ctrl-C. The bot closes the Discord gateway, cancels
  background tasks, and releases the instance lock.
- All pending outbox records remain in SQLite and will be retried on next start.
