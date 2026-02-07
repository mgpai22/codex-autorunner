# Discord Architecture

## Overview

The Discord integration is a gateway-based bot that bridges Discord servers to the
Codex app-server. It runs as a long-lived process (`car discord start`) and uses the
Discord Gateway (WebSocket) via `discord.py>=2.3` to receive events, route them
through CAR, and stream responses back as embeds and messages. This is separate from
the lightweight `notifications.discord` webhook settings (which only send one-way
notifications).

## Design principles

| Principle | Discord implementation |
|-----------|----------------------|
| Gateway over polling | Discord requires a persistent WebSocket for interactive bots; `discord.py` manages reconnection and heartbeats |
| Slash commands over prefix | Discord is deprecating prefix commands for verified bots; slash commands offer auto-complete, type validation, and discoverability |
| Embeds for structured output | Rich embeds (4096-char description, color-coded, fields) for status/progress; plain text for agent responses |
| Thread per task | Discord threads mirror Telegram forum topics: isolation, notification control, archival |
| Forum-backed tasks | Each task is a forum post/thread with tags for state — see `docs/discord/swarm-surface.md` |
| Mixin composition | `DiscordBotService` composes 16 mixin classes for separation of concerns |

## Architecture layers

```
[ Surface: CLI discord_app ]
        |
[ Adapter: DiscordBotService (gateway bot) ]
        |
[ Integration: discord.py, app_server_supervisor, opencode_supervisor ]
        |
[ Control Plane: SQLite state, filesystem config ]
        |
[ Engine: Codex app-server ]
```

## Configuration and inputs

Config lives under `discord_bot` in `codex-autorunner.yml` and the generated
`.codex-autorunner/config.yml`:

- `discord_bot.enabled`: turn the bot on.
- `discord_bot.bot_token_env`: env var name that holds the bot token (default `CAR_DISCORD_BOT_TOKEN`).
- `discord_bot.allowed_guild_ids`: allowlist of Discord guild (server) IDs.
- `discord_bot.allowed_channel_ids`: allowlist of channel IDs (threads check parent channel).
- `discord_bot.allowed_role_ids`: allowlist of role IDs (user must have at least one).
- `discord_bot.allowed_user_ids`: allowlist of user IDs.
- `discord_bot.trigger_mode`: `mentions` (default) or `all`.
- `discord_bot.defaults`: approval/sandbox defaults for the app-server client.
- `discord_bot.concurrency`: `max_parallel_turns` (default 4), `per_topic_queue`.
- `discord_bot.progress_stream`: live embed update settings (`enabled`, `min_edit_interval_seconds`).
- `discord_bot.media`: image/file handling limits and prompts.
- `discord_bot.shell`: `!<cmd>` settings (enable flag, timeouts, output limits).
- `discord_bot.cache`: in-memory TTLs for pending approvals, selections, and progress state.
- `discord_bot.app_server_command(_env)`: how to launch `codex app-server`.
- `discord_bot.app_server`: app-server tuning (`max_handles`, `idle_ttl_seconds`, `turn_timeout_seconds`).

Required env vars:

- `CAR_DISCORD_BOT_TOKEN`
- `CAR_DISCORD_GUILD_IDS` (optional convenience, comma-separated)
- `CAR_DISCORD_CHANNEL_IDS` (optional convenience)
- `CAR_DISCORD_APP_SERVER_COMMAND` (optional override)

At minimum, `allowed_guild_ids` must be non-empty for the bot to start.

## Module layout

```
integrations/discord/
  __init__.py              # Public exports: DiscordBotService, DiscordBotConfig, DiscordStateStore
  adapter.py               # discord.py Bot wrapper, allowlist enforcement, component builders
  api_types.py             # Thin dataclasses: DiscordContext
  config.py                # DiscordBotConfig frozen dataclass + from_raw() factory
  constants.py             # Platform limits, timeouts, embed colors, defaults
  dispatch.py              # Interaction/message routing, component dispatch
  doctor.py                # Health check diagnostics (gateway, state, token validation)
  helpers.py               # Topic key builder, formatting utilities
  notifications.py         # Lifecycle event delivery to channels
  outbox.py                # DiscordOutboxManager (queue + retry + coalescing)
  overflow.py              # Message splitting for 2000-char limit
  progress_stream.py       # Live embed progress tracking (1.5s edit interval)
  rendering.py             # Embed builders (status, response, error, repos, progress)
  runtime.py               # DiscordRuntimeHelpers mixin (workspace/client resolution)
  service.py               # DiscordBotService main class (mixin composition, lifecycle)
  state.py                 # DiscordStateStore (SQLite), topic records, schema
  transport.py             # DiscordMessageTransport mixin (send/edit/delete)
  trigger_mode.py          # Mention-based trigger logic
  types.py                 # PendingApproval, PendingQuestion, TurnContext, TurnKey
  handlers/
    __init__.py
    approvals.py           # DiscordApprovalHandlers mixin (Future-based blocking)
    callbacks.py           # Button/select interaction dispatch
    messages.py            # Free-text message handling, coalescing
    questions.py           # DiscordQuestionHandlers mixin (select + modal)
    selections.py          # Paginated selection menus
    agent_bus.py           # DiscordAgentBusMixin (lifecycle + coordination bus)
    dashboard.py           # DiscordDashboardMixin (pinned dashboard embed)
    rbac.py                # DiscordRBACMixin (role-based access control)
    tasks.py               # DiscordTasksMixin (forum task cards + triage commands)
    commands/
      __init__.py          # Exports all command mixins
      execution.py         # /run, /stop, /new command implementations
      formatting.py        # FormattingHelpers mixin (consistent embed styling)
      scaffold.py          # ScaffoldCommands mixin (/setup implementation)
      shared.py            # SharedHelpers mixin (turn resolution, interrupt)
      workspace.py         # /bind, /status command implementations
    commands_runtime.py    # DiscordCommandHandlers mixin (CommandTree registration)
    commands_spec.py       # SlashCommandSpec registry
```

## Service composition

`DiscordBotService` inherits from 16 mixin classes:

```python
class DiscordBotService(
    DiscordRuntimeHelpers,        # Workspace/client resolution, env builder
    DiscordMessageTransport,      # Message send/edit/delete
    DiscordNotificationHandlers,  # Lifecycle event delivery
    DiscordApprovalHandlers,      # Approval request handling (Future-based)
    DiscordQuestionHandlers,      # Question request handling (Select + Modal)
    DiscordSelectionHandlers,     # Paginated selection menus
    DiscordCommandHandlers,       # Slash command registration on CommandTree
    SharedHelpers,                # Turn resolution and interrupt helpers
    ExecutionCommands,            # /run, /stop, /new implementations
    WorkspaceCommands,            # /bind, /status implementations
    FormattingHelpers,            # Embed formatting utilities
    ScaffoldCommands,             # /setup scaffolding for swarm control surface
    DiscordRBACMixin,             # Role-based access control
    DiscordAgentBusMixin,         # Lifecycle/coordination event bus posting
    DiscordDashboardMixin,        # Pinned dashboard embed maintenance
    DiscordTasksMixin,            # Forum-backed task cards and triage commands
):
```

This mirrors the Telegram integration's mixin pattern but with Discord-specific
handler implementations. See `docs/discord/swarm-surface.md` for details on the
last 5 mixins (swarm control surface).

## Runtime flow

1. `car discord start --path <repo_or_hub>` loads config and starts the gateway.
2. `DiscordBotClient` connects to the Discord Gateway (WebSocket) and receives events.
3. On `on_ready`: register PersistentViews (for approval button survival), sync slash
   commands to all allowed guilds, restore pending state.
4. On `on_message`: allowlist check, trigger mode check, route to `handle_message()`.
   Messages in bound channels are coalesced (default 0.5s window, max 20 messages or
   10s) then dispatched as agent turns.
5. On `on_interaction`: allowlist check, route to `dispatch_interaction()`. Slash
   commands are handled by the CommandTree; component interactions (buttons, selects,
   modals) are dispatched to approval/question/selection handlers.
6. All slash commands `defer()` immediately to meet the 3-second interaction deadline.
7. Responses stream back to Discord as embed edits (progress) and final messages.

## State and persistence

Per-guild/channel/thread state is stored in `.codex-autorunner/discord_state.sqlite3`
(schema version 2) with tables:

- `discord_meta`: schema version, timestamps
- `discord_topics`: workspace binding, active thread ID, approval mode per topic
- `discord_channel_bindings`: static channel-to-workspace mappings
- `discord_pending_approvals`: approval requests that survive restarts
- `discord_outbox`: messages queued for delivery with retry state
- `discord_scaffolded_channels`: channels created by `/setup` (guild, workspace, type → channel_id)
- `discord_forum_tags`: forum tag name → Discord tag ID mappings
- `discord_tasks`: per-task state (thread_id, state, prompt, root_message_id)
- `discord_alerts`: alert deduplication (last_sent_at per task+type)
- `discord_agent_webhooks`: per-agent webhook credentials for the agent bus
- `discord_dashboard`: pinned dashboard message tracking

Topic key format: `"{guild_id}:{channel_id}:{thread_id_or_root}"`

See `docs/discord/swarm-surface.md#14-state-store-schema-v2` for full schemas.

## Concurrency model

- A global `asyncio.Semaphore(max_parallel_turns)` (default 4) limits concurrent
  agent turns across all channels.
- Per-topic queuing ensures at most one turn runs per conversation context.
- Background tasks: outbox flush loop, cache cleanup, spawned turn tasks.
- The outbox provides reliable delivery with coalescing and retry (immediate retries
  at 0.5s/2s/5s, then background at 10s intervals, max 8 attempts).

## Interactive components

| Component | Discord primitive | Use case |
|-----------|------------------|----------|
| Approval buttons | `PersistentView` with 4 `Button`s | Accept / Accept Session / Decline / Cancel Turn |
| Question select | `Select` menu + `Modal` for "Other" | Agent question with options or free-text |
| Paginated lists | `Select` + Previous/Next `Button`s | Model picker, repo picker, resume picker |

All components use `custom_id` encoding (max 100 chars) in the format
`{type}:{request_id}:{action}`. PersistentViews are re-registered in `on_ready()`
so approval buttons survive bot restarts.

## Slash commands

| Command | Description |
|---------|-------------|
| `/run <prompt>` | Start an agent task |
| `/stop` | Interrupt the active task |
| `/new` | Start a new conversation (clear thread context) |
| `/resume [thread_id]` | Resume a previous conversation |
| `/bind <workspace>` | Bind channel to a workspace directory |
| `/repos` | List available repositories |
| `/status` | Show current workspace, agent, model, approval mode |
| `/model [name]` | Show or change the model |
| `/agent [name]` | Show or change the agent backend |
| `/approvals [mode]` | Set approval and sandbox policy |
| `/health` | Run health diagnostics |
| `/setup` | Scaffold swarm control surface channels (see swarm-surface.md) |
| `/workspaces` | List scaffolded workspaces with channel links |
| `/tasks list` | List tasks filtered by workspace/tag |
| `/tasks mine` | List tasks created by the caller |
| `/review` | Run a code review |
| `/flow` | Ticket flow controls |
| `/compact` | Generate a conversation summary |
| `/files` | View inbox/outbox files |

Commands are registered on the discord.py `CommandTree` and synced to each allowed
guild for instant availability (no 1-hour global sync delay).

## Observability

The bot logs structured events (e.g., `discord.bot.ready`, `discord.run.started`,
`discord.turn.completed`, `discord.allowlist.denied`) to the hub log path (default
`.codex-autorunner/codex-autorunner-hub.log`). See `docs/ops/discord-bot-runbook.md`
for troubleshooting.

## Quickstart (high level)

1. Create a Discord bot application at https://discord.com/developers/applications.
2. Enable privileged intents: **Server Members** and **Message Content**.
3. Invite the bot to your server with `bot` + `applications.commands` scopes.
4. Set env vars: `CAR_DISCORD_BOT_TOKEN=<token>`.
5. Configure `discord_bot.enabled: true` and `discord_bot.allowed_guild_ids` in
   `codex-autorunner.yml`.
6. Run `car discord start --path <repo_or_hub>` and use `/status` or `/health`.

## Comparison with Telegram integration

| Aspect | Telegram | Discord |
|--------|----------|---------|
| Transport | Long-polling (Bot API HTTP) | Gateway (WebSocket) |
| Commands | `/command` text parsing | Slash commands (CommandTree) |
| Approvals | Inline keyboard buttons | PersistentView buttons |
| Questions | Reply keyboard + text input | Select menu + Modal |
| Message limit | 4096 chars | 2000 chars (embeds: 4096) |
| Progress updates | Text edit (1.0s interval) | Embed edit (1.5s interval) |
| Access control | chat_id + user_id | guild_id + channel_id + role_id + user_id |
| State file | `telegram_state.sqlite3` | `discord_state.sqlite3` |
| Topic key | `{chat_id}:{thread_id\|root}` | `{guild_id}:{channel_id}:{thread_id\|root}` |
| Instance lock | SHA256(bot_token) | SHA256(bot_token) |

## References

- `docs/discord/swarm-surface.md` — Swarm control surface (scaffolding, forum tasks, dashboard, RBAC, agent bus)
- `docs/discord/security.md`
- `docs/discord/discord-integration.md`
- `docs/ops/discord-bot-runbook.md`
- `docs/car_constitution/20_ARCHITECTURE_MAP.md`
