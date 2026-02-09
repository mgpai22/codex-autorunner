# Discord Bot Security Posture

This document describes the security surface and operational posture of the
interactive Discord bot integration. It is intended for operators and agents
who want to understand what the bot can do, which controls exist, and the
tradeoffs involved.

## Scope and threat model

- The bot is a gateway client for the Discord API. It maintains a persistent
  WebSocket connection to Discord and does not expose an inbound HTTP endpoint.
- Access control is allowlist-based across four dimensions: guild IDs, channel
  IDs, role IDs, and user IDs.
- The bot can run Codex turns and, optionally, shell commands within a bound
  workspace. This can result in code execution on the host.
- Discord messages are not end-to-end encrypted. Treat Discord as a transport,
  not a secret store.

## Trust boundaries

- **Discord Gateway API**: All messages, interactions, and component events
  originate from Discord. The bot trusts Discord to authenticate users but
  still enforces its own allowlists.
- **Codex app-server**: The bot proxies messages to a local Codex app-server
  process that executes turns and tools on the host.
- **Local filesystem**: The bot reads/writes state and stores uploads within
  the bound workspace.

## Authentication and allowlists

The bot enforces a four-dimensional allowlist (`DiscordAllowlist`):

| Dimension | Config key | Env var | Behavior when empty |
|-----------|-----------|---------|---------------------|
| Guild IDs | `allowed_guild_ids` | `CAR_DISCORD_GUILD_IDS` | **Required** - bot refuses to start |
| Channel IDs | `allowed_channel_ids` | `CAR_DISCORD_CHANNEL_IDS` | Unrestricted (all channels in allowed guilds) |
| Role IDs | `allowed_role_ids` | - | Unrestricted (all roles) |
| User IDs | `allowed_user_ids` | - | Unrestricted (all users in allowed guilds) |

Allowlist checks are enforced for:
- All incoming messages (`on_message`)
- All interactions: slash commands, button clicks, select menus, modals
  (`on_interaction`)
- Thread messages check the **parent channel** against `allowed_channel_ids`

### How allowlist_allows() works

For each event, the function extracts `guild_id`, `channel_id` (parent for
threads), `user_id`, and `member_role_ids`, then checks each non-empty
allowlist dimension. If any populated dimension fails, the event is rejected.

## Privileged intents

The bot requires these privileged intents (must be enabled in the Discord
Developer Portal):

| Intent | Purpose |
|--------|---------|
| `guilds` | Guild metadata and channel visibility |
| `guild_messages` | Receive messages in guild channels and threads |
| `message_content` | Read message text for trigger mode and prompt extraction |
| `members` | Role-based access control (check user roles against allowlist) |

Without `message_content`, the bot cannot read message text. Without `members`,
role-based allowlisting is unavailable.

## Execution surface

- Normal messages in bound channels are forwarded to the Codex app-server for
  tool execution (subject to trigger mode).
- `/approvals` controls the approval mode and policies per topic.
- The default approval mode is `yolo`, which is equivalent to:
  - `approval_policy = never`
  - `sandbox_policy = dangerFullAccess`
- If `discord_bot.shell.enabled` is true, `!<cmd>` runs `bash -lc` in the bound
  workspace through the app-server.

## Workspace binding

- `/bind <path>` lets an allowed user bind a channel to a workspace directory.
- The path must exist and be a directory on the host filesystem. Paths are
  resolved via `Path.expanduser().resolve()` and validated with `is_dir()`.
- Once bound, the bot can read files and run commands in that workspace, subject
  to the configured approval/sandbox policy.
- Bindings are stored in SQLite (`discord_channel_bindings` table) keyed by
  `{guild_id}:{channel_id}`.

## Message coalescing bounds

To prevent abuse via rapid message flooding:

- **Max buffer size**: 20 messages per topic before forced flush
  (`MAX_COALESCE_BUFFER_MESSAGES`)
- **Max delay**: 10 seconds from first message before forced flush
  (`MAX_COALESCE_DELAY_SECONDS`)
- **Default window**: 0.5 seconds of quiet before normal flush

These bounds prevent a user from indefinitely stalling the bot or growing the
buffer without limit.

## Interactive component security

- Button and select `custom_id` values are encoded as
  `{type}:{request_id}:{action}` (max 100 chars per Discord limit).
- Request IDs are server-generated UUIDs; clients cannot forge valid IDs.
- Approval futures timeout after 300 seconds (configurable via
  `pending_approval_ttl_seconds`).
- PersistentViews survive bot restarts; stale approvals are restored from the
  `discord_pending_approvals` SQLite table on `on_ready()`.

## Instance locking

Only one bot instance per token can run at a time:

- Lock file path is derived from `SHA256(bot_token)`.
- Lock file contains PID, hostname, start time, and config root.
- On startup, if the lock exists and the PID is alive, startup is rejected
  (`DiscordBotLockError`).
- Stale locks (dead PID) are automatically cleaned up.

## Data at rest and logs

- Per-topic state is stored in `.codex-autorunner/discord_state.sqlite3`,
  including workspace paths, thread IDs, approval state, and outbox records.
- Logs include guild IDs, channel IDs, user IDs, and event metadata; review
  your log retention and access controls accordingly.
- No message content is persisted in the state database; only metadata
  (topic keys, approval decisions, outbox delivery status).

## Rate limit awareness

Discord enforces strict rate limits:

| Resource | Limit | Bot mitigation |
|----------|-------|---------------|
| Message edits | ~5 per 5s per channel | Progress edit interval: 1.5s (max 3-4 edits per 5s) |
| Message sends | 5 per 5s per channel | Outbox coalescing reduces volume |
| Interaction response | 3 seconds | All handlers `defer()` immediately |
| Select options | 25 max | Paginated selection with Previous/Next |
| Embed description | 4096 chars | Overflow to file attachment for longer content |
| Message content | 2000 chars | Split, embed fallback, or file attachment |

## Recommendations

- Treat Discord as a convenience interface, not a secure enclave.
- Keep `allowed_guild_ids` and `allowed_channel_ids` narrow.
- Prefer `approval_mode = safe` and a restrictive sandbox for day-to-day use.
- Disable `discord_bot.shell.enabled` unless you explicitly need `!<cmd>`.
- Use `trigger_mode: mentions` in shared channels to avoid reacting to every
  message.
- Use per-user bot tokens for multi-operator setups when possible.
- Enable `allowed_role_ids` for role-based access in servers with many users.
- Monitor logs for `discord.allowlist.denied` and `discord.turn.failed` events.

## References

- `docs/discord/architecture.md`
- `docs/discord/discord-integration.md`
- `docs/ops/discord-bot-runbook.md`
