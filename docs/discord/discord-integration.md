# Discord Integration Specification

This document describes the normative specifications for the Discord integration,
including dispatch behavior, outbox operations, trigger mode, component interactions,
and security controls.

## Outbox Specification

The Discord outbox provides reliable message delivery with retry logic, coalescing,
and per-channel scheduling.

### Outbox Records

An `OutboxRecord` represents a pending message to be delivered:

- `record_id`: Unique identifier for the outbox record
- `channel_id`: Target Discord channel ID
- `thread_id`: Optional thread ID
- `reply_to_message_id`: Optional message ID to reply to
- `placeholder_message_id`: Optional placeholder message ID to delete after delivery
- `text`: Message content to send
- `created_at`: ISO timestamp of record creation
- `attempts`: Number of delivery attempts made (default 0)
- `last_error`: Last error message (truncated to 500 chars)
- `last_attempt_at`: ISO timestamp of last attempt
- `next_attempt_at`: ISO timestamp for next retry (optional)
- `operation`: Type of operation (e.g., "send", default "send")
- `message_id`: ID of sent message (populated after success)
- `outbox_key`: Optional key for coalescing records

### Outbox Keys and Coalescing

Records are coalesced by `outbox_key`. When multiple records share the same
`outbox_key`, only the most recent is delivered. Older duplicates are discarded.

### Retry Behavior

The outbox uses a two-phase retry strategy:

1. **Immediate retries**: For transient failures, retry with delays:
   - Attempt 1: 0.5s delay
   - Attempt 2: 2.0s delay
   - Attempt 3: 5.0s delay
   - Then proceed to scheduled retry

2. **Scheduled retries**: After immediate retries are exhausted:
   - Wait for `next_attempt_at` timestamp
   - Retry every `OUTBOX_RETRY_INTERVAL_SECONDS` (default 10s) if ready

3. **Give up**: After `OUTBOX_MAX_ATTEMPTS` (default 8) failed attempts, the
   record is abandoned and all records with the same `outbox_key` are deleted.

### Inflight Tracking

The outbox tracks messages inflight using `outbox_key` (or `record_id`). Only one
record with a given key can be processed at a time.

## Trigger Mode Specification

### Modes

- `mentions` (default): Only respond when the bot is @mentioned or replied to
- `all`: Respond to every message in allowed channels

### Mentions Mode Triggering Conditions

A message triggers a run in `mentions` mode if ANY of the following are true:

1. **Explicit @mention**: The bot's user ID appears in `message.mentions`
2. **Reply to bot message**: The message is a reply to a message authored by the
   bot (`message.reference.resolved.author.id == bot_user_id`)

When triggered via mention, the bot mention prefix (`<@bot_id>` or `<@!bot_id>`)
is stripped from the message content before forwarding to the agent.

### Default trigger mode

Unlike Telegram (which defaults to `all`), Discord defaults to `mentions` because
Discord bots are commonly added to shared servers where responding to every message
would be disruptive.

## Dispatch Specification

### Interaction Dispatch

Interactions are routed by type:

1. **Application commands** (type 2): Handled automatically by the discord.py
   `CommandTree`. All registered slash commands defer immediately, then invoke
   their handler.
2. **Component interactions** (type 3): Button clicks and select menu choices.
   Routed to `dispatch_component_interaction()` which parses `custom_id` and
   delegates to the appropriate handler (approval, question, or selection).
3. **Modal submissions** (type 5): Free-text input from "Other" modals. Routed
   to `dispatch_modal_submit()` which resolves the pending question future.

### Message Dispatch

Messages are routed through `handle_message()`:

1. Check trigger mode and allowlist
2. Determine topic key from `{guild_id}:{channel_id}:{thread_id_or_root}`
3. Check for existing channel binding
4. Apply message coalescing (combine rapid messages into one prompt)
5. Dispatch combined text as an agent turn

### Message Coalescing

Messages arriving within the coalesce window (default 0.5s) are combined:

- **Window**: `coalesce_window_seconds` (default 0.5s) of quiet before flush
- **Max buffer**: `MAX_COALESCE_BUFFER_MESSAGES` (20) messages before forced flush
- **Max delay**: `MAX_COALESCE_DELAY_SECONDS` (10s) from first message before
  forced flush
- Combined messages are joined with newlines

## Component Interaction Specification

### Approval Flow

When the Codex app-server requests approval:

1. Bot creates an embed describing the approval request
2. Attaches a `PersistentView` with four buttons:
   - **Accept** (`approval:{request_id}:accept`) - green
   - **Accept Session** (`approval:{request_id}:accept_session`) - blue
   - **Decline** (`approval:{request_id}:decline`) - red
   - **Cancel Turn** (`approval:{request_id}:cancel`) - grey
3. Creates an `asyncio.Future` and stores it in `_pending_approvals`
4. Awaits `asyncio.wait_for(future, timeout=300)`
5. On button click: resolves the future with the decision
6. On timeout: cancels the future and cleans up

Approval state is persisted to `discord_pending_approvals` in SQLite so that
buttons survive bot restarts. PersistentViews are re-registered in `on_ready()`.

### Question Flow

When the agent asks a question with options:

1. Bot creates an embed describing the question
2. Attaches a view with:
   - `Select` menu for predefined options (max 25 per Discord limit)
   - **Other** button that opens a `Modal` with a `TextInput` for free text
   - **Done** button (for multi-select questions)
   - **Cancel** button
3. Same Future-based blocking pattern as approvals

### Paginated Selection

For long option lists (repos, models, threads):

1. Items are split into pages of 10 (configurable)
2. Each page shows a `Select` menu with up to 10 options
3. **Previous** / **Next** buttons navigate pages
4. Page indicator shows current position (e.g., "2/5")
5. **Cancel** button dismisses the selection

## Slash Command Specification

### Registration

Commands are registered on the discord.py `CommandTree` in
`DiscordCommandHandlers._register_slash_commands()`. On `on_ready()`, commands are
synced to each guild in `allowed_guild_ids` for instant availability.

### Command List

| Command | Parameters | Description |
|---------|-----------|-------------|
| `/run` | `prompt: str`, `model: str?`, `effort: str?` | Start an agent task with the given prompt. Optional `model` (default: `gpt-5.3-codex`) and `effort` (default: `medium`) override for this invocation. Both support autocomplete. |
| `/stop` | - | Interrupt the active task in this context |
| `/new` | - | Clear thread context and start a new conversation |
| `/resume` | `thread_id: str?` | Resume a previous conversation |
| `/bind` | `workspace: str?` | Bind channel to a workspace directory (show current if no arg) |
| `/repos` | - | List available repositories from the hub |
| `/status` | - | Show current workspace, agent, model, effort, approval mode |
| `/model` | `name: str?` | Show or switch the model |
| `/effort` | `level: str?` | Show or switch the reasoning effort (thinking level) |
| `/agent` | `name: str?` | Show or switch the agent backend |
| `/approvals` | `mode: str?` | Set approval and sandbox policy (safe/yolo) |
| `/health` | - | Run health diagnostics |
| `/workspace create` | - | Create a new workspace (interactive modal flow) |
| `/workspace clone` | `url: str`, `name: str?` | Clone a git repo into a new workspace (direct) |
| `/workspaces` | - | List scaffolded workspaces with channel links |
| `/swarm` | `prompt: str`, `preset: str?` | Start a multi-agent swarm. Default preset: `code-review`. |
| `/swarm-stop` | `swarm_id: str?` | Stop an active swarm (all swarms if no ID given) |
| `/swarm-status` | `swarm_id: str?` | Show swarm status (latest swarm if no ID given) |

### Interaction Deadline

All slash command handlers call `interaction.response.defer()` immediately to meet
the 3-second interaction deadline. Long-running operations use `followup.send()`
for responses.

## Agent Invariants

### State Persistence

- All per-topic state is persisted to `discord_state.sqlite3`
- State includes: workspace binding, active thread, approval mode, outbox records
- State survives bot restarts and process crashes

### Topic Keys Are Unique

- A `topic_key` uniquely identifies a conversation context
- Format: `"{guild_id}:{channel_id}:{thread_id_or_root}"`
- The `root` suffix is used when a message is in a channel (not a thread)

### At-Most-One Active Turn Per Topic

- Only one agent turn runs at a time for a given `topic_key`
- Additional messages are coalesced or queued
- The global semaphore (default 4) limits total concurrent turns

### Outbox Exactly-Once Semantics

- A given `outbox_key` is delivered at most once
- After successful delivery, all records with that key are deleted
- Duplicate records are coalesced (latest wins)

### Allowlist Enforcement

- All incoming events (messages and interactions) are validated against the
  four-dimensional allowlist before any processing
- Events that fail any populated allowlist dimension are silently dropped
  (logged as `discord.allowlist.denied`)

## Approval Presets

The `/approvals` command sets approval mode via presets:

- `read-only`: `approval_policy=on-request`, `sandbox_policy=readOnly`
- `auto`: `approval_policy=on-request`, `sandbox_policy=workspaceWrite`
- `full-access`: `approval_policy=never`, `sandbox_policy=dangerFullAccess`

Default approval mode is `yolo`, which maps to `full-access`.

## Discord Platform Limits

| Limit | Value | Mitigation |
|-------|-------|-----------|
| Message length | 2000 chars | Split into chunks, use embeds (4096), or file attachment |
| Embed description | 4096 chars | Truncate or attach as file |
| Embed title | 256 chars | Truncate |
| Embed fields | 25 max | Paginate |
| Embed field name | 256 chars | Truncate |
| Embed field value | 1024 chars | Truncate |
| Select options | 25 max | Paginate with Previous/Next |
| Custom ID | 100 chars | Compact encoding: `{type}:{request_id}:{action}` |
| Interaction timeout | 3 seconds | Always `defer()` first |
| Thread auto-archive | 60/1440/4320/10080 min | Default 1440 (24h) |
| File attachments | 10 per message | Batch if needed |
| Edit rate limit | ~5 per 5s per channel | Progress edit interval: 1.5s |
