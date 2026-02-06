# Discord-Native Agent Swarm Control Surface

## Context

The Discord bot currently treats Discord as a simple chat interface — users manually `/bind` channels, manually create threads, and get a single notification channel. Discord offers much richer primitives (categories, forums, threads, roles, webhooks, presence) that map naturally onto agent swarm concepts. This plan transforms the Discord integration into a full swarm control surface where the server structure *is* the swarm topology.

## Overview

7 features across 4 new mixins, 3 new SQLite tables, and modifications to ~10 existing files.

---

## 1. Server Scaffolding (`/setup`)

**What**: A `/setup` command that auto-creates a Discord server structure mirroring the hub's workspaces.

**Target structure**:
```
[Category: frontend-app]
  #tasks           -- Bound to workspace. Users /run here. Auto-threaded.
  #activity-feed   -- Read-only. Rich embeds for every agent action.
  #approvals       -- Approval requests with buttons.

[Category: backend-api]
  #tasks
  #activity-feed
  #approvals

[Category: Control Plane]
  #dashboard       -- Pinned embed: real-time workspace status grid.
  #agent-bus       -- PMA decisions, cross-workspace handoffs, lifecycle.
  #notifications   -- General lifecycle events.
```

**New config** in `config.py`:
```python
@dataclass(frozen=True)
class DiscordScaffoldConfig:
    enabled: bool = False
    auto_bind: bool = True
    category_prefix: str = ""
    task_channel_name: str = "tasks"
    activity_channel_name: str = "activity-feed"
    approval_channel_name: str = "approvals"
    dashboard_channel_name: str = "dashboard"
    agent_bus_channel_name: str = "agent-bus"
    notifications_channel_name: str = "notifications"
```

**New state table** `discord_scaffolded_channels`:
```sql
CREATE TABLE IF NOT EXISTS discord_scaffolded_channels (
    guild_id INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,  -- 'category'|'tasks'|'activity'|'approvals'|'dashboard'|'agent_bus'|'notifications'
    discord_channel_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, workspace_id, channel_type)
)
```

**Idempotency**: Before creating, check table + verify channel still exists via `bot.fetch_channel()`. If manually deleted, recreate. If category with same name exists, reuse it.

**Implementation**: New mixin `ScaffoldCommands` in `handlers/commands/scaffold.py`.

**Files**:
| File | Action |
|------|--------|
| `handlers/commands/scaffold.py` | **Create** — `ScaffoldCommands` mixin |
| `config.py` | Modify — add `DiscordScaffoldConfig`, parse from `scaffold` key |
| `state.py` | Modify — add table + CRUD methods |
| `commands_runtime.py` | Modify — register `/setup` command |
| `service.py:102-114` | Modify — add `ScaffoldCommands` to mixin list |
| `rendering.py` | Modify — add `build_setup_summary_embed()` |

---

## 2. Thread-Per-Task (Auto-Threading)

**What**: Every `/run` and every mention-triggered turn auto-creates a Discord thread. The thread becomes the isolated execution context.

**Thread naming**: `task-{prompt[:80]}` sanitized (remove newlines, special chars). Discord limit: 100 chars. Fallback: `task-{timestamp}`.

**Changes to `_cmd_run_impl()`** (`execution.py:216`):

Current flow creates a topic in-place. New flow:
1. After checking binding (line 241), create a Discord thread:
   ```python
   parent = bot.get_channel(channel_id)
   thread_result = await parent.create_thread(
       name=sanitize_thread_name(prompt),
       auto_archive_duration=1440,  # 24h
   )
   thread = thread_result.thread
   ```
2. Build topic_key with `thread.id` as the thread_id
3. Send the followup into the thread (not parent channel)
4. Spawn `_execute_turn()` with the thread context

**Thread lifecycle**:
- On completion: edit thread name to prefix status icon (checkmark/X)
- `auto_archive_duration=1440` handles cleanup (Discord archives after 24h inactivity)
- On `/stop`: prefix thread name with "stopped-"

**Changes to `_execute_turn()`** (`execution.py:31`):
- After step 12 (turn completed), rename thread: `await thread.edit(name=f"done-{original_name[:95]}")`
- In error/timeout handlers, rename to `fail-{name}` or `timeout-{name}`

**Changes to message handler** (`messages.py`):
- When a mention triggers in a non-thread context (parent `#tasks` channel), create thread before dispatching

**Files**:
| File | Action |
|------|--------|
| `handlers/commands/execution.py` | Modify — auto-create thread in `_cmd_run_impl`, rename on completion in `_execute_turn` |
| `handlers/messages.py` | Modify — auto-thread for mention-triggered turns |
| `helpers.py` | Modify — add `sanitize_thread_name(prompt) -> str` |
| `constants.py` | Modify — add `THREAD_NAME_MAX_LEN = 100` |
| `state.py` | Modify — add `list_topics_for_channel()` method |

---

## 3. Role-Based Access Control (RBAC)

**What**: Map Discord roles to permission tiers that control command access and approval modes.

**New config**:
```yaml
discord_bot:
  rbac:
    enabled: true
    tiers:
      - name: admin
        role_ids: [123456789]
        approval_mode: yolo
        can_setup: true
        can_run: true
        can_stop: true
        can_bind: true
      - name: dev
        role_ids: [987654321]
        approval_mode: safe
        can_run: true
        can_stop: true
        can_bind: true
      - name: viewer
        role_ids: []
        read_only: true
    default_tier: viewer
```

**Config dataclasses** in `config.py`:
```python
@dataclass(frozen=True)
class DiscordRoleTier:
    name: str
    role_ids: set[int]
    approval_mode: str = "safe"
    can_run: bool = True
    can_setup: bool = False
    can_bind: bool = True
    can_stop: bool = True
    read_only: bool = False

@dataclass(frozen=True)
class DiscordRBACConfig:
    enabled: bool = False
    tiers: list[DiscordRoleTier] = field(default_factory=list)
    default_tier: str = "viewer"
```

**Enforcement**: New mixin `DiscordRBACMixin` in `handlers/rbac.py` provides:
- `_resolve_user_tier(member) -> DiscordRoleTier` — first matching tier by role
- `_enforce_permission(interaction, perm: str) -> bool` — check + ephemeral denial

Added at the top of each command handler (`_handle_slash_run` checks `can_run`, etc.).

**Interaction with allowlist**: Allowlist remains the first-pass gate (guild/channel access). RBAC is second-pass (capability control within allowed contexts).

**Approval mode resolution priority**: explicit topic override > RBAC tier > config default.

**Channel permission overwrites**: When `/setup` creates channels, apply Discord permission overwrites:
- `#activity-feed`, `#dashboard`, `#agent-bus`: `send_messages=False` for `@everyone`, `send_messages=True` for bot
- `#tasks`: `send_messages=False` for viewer-tier roles

**Files**:
| File | Action |
|------|--------|
| `handlers/rbac.py` | **Create** — `DiscordRBACMixin` |
| `config.py` | Modify — add `DiscordRBACConfig`, `DiscordRoleTier` |
| `service.py:102-114` | Modify — add mixin |
| `commands_runtime.py` | Modify — permission checks in each handler |
| `handlers/commands/execution.py` | Modify — pass user tier to policy resolution |
| `handlers/commands/scaffold.py` | Modify — apply permission overwrites on creation |

---

## 4. Agent Communication Bus

**What**: A `#agent-bus` channel showing all cross-agent coordination as structured embeds, with webhook-per-agent for distinct identities.

### 4a. Lifecycle Event Bridge

Register a listener with `LifecycleEventEmitter` (in `core/lifecycle_events.py`) during `on_ready`:
```python
lifecycle_emitter.add_listener(lambda event:
    asyncio.run_coroutine_threadsafe(
        self._post_to_agent_bus(event), self._bot.bot.loop
    )
)
```

Route events to `#agent-bus` as color-coded embeds.

### 4b. PMA Decision Posting

`LifecycleEventType.DISPATCH_CREATED` events already fire when PMA creates dispatches. The listener catches these and posts structured embeds showing source workspace, target, decision summary, and action items.

### 4c. Cross-Workspace Handoff Signals

When PMA routes a task from workspace A to workspace B (via `_enqueue_pma_for_lifecycle_event` in `hub.py`), post a handoff embed:
```
[Handoff] frontend-app -> backend-api
"API endpoint needs updating to match new frontend schema"
```

### 4d. Webhook-Per-Agent Identity

New state table `discord_agent_webhooks`:
```sql
CREATE TABLE IF NOT EXISTS discord_agent_webhooks (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    webhook_id INTEGER NOT NULL,
    webhook_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id, agent_name)
)
```

Agent identities:
- **PMA**: "Project Manager" — posts decisions, orchestration moves
- **Codex**: "Codex Agent" — posts task updates
- **System**: "Autorunner" — posts lifecycle events

Each posts via its own webhook with distinct name/avatar.

**New config fields** in `DiscordBotConfig`:
```python
agent_bus_channel_id: Optional[int] = None
```

Channel resolution: scaffolded table > explicit config > `default_notification_channel_id`.

**Files**:
| File | Action |
|------|--------|
| `handlers/agent_bus.py` | **Create** — `DiscordAgentBusMixin` |
| `state.py` | Modify — add `discord_agent_webhooks` table |
| `config.py` | Modify — add `agent_bus_channel_id` |
| `rendering.py` | Modify — add `build_bus_lifecycle_embed()`, `build_bus_dispatch_embed()`, `build_bus_handoff_embed()` |
| `service.py` | Modify — add mixin, register lifecycle listener in startup |
| `notifications.py` | Modify — route lifecycle events to bus in addition to notification channel |

---

## 5. Live Dashboard

**What**: A pinned embed in `#dashboard` showing real-time status of all workspaces. Updated on turn start/complete/error.

**Dashboard embed layout**:
```
Title: "Agent Swarm Dashboard"
Description: "Last updated: 2025-01-15 14:30:02 UTC"

Field: "frontend-app" | Idle | 14 turns | Last: 2m ago
Field: "backend-api"  | Running: fix auth bug | Context: 45%
Field: "ml-pipeline"  | Error (timeout) | Last: 15m ago

Footer: "3 workspaces | 1 active | 27 turns today"
```

**New state table** `discord_dashboard`:
```sql
CREATE TABLE IF NOT EXISTS discord_dashboard (
    guild_id INTEGER NOT NULL PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    pinned_at TEXT NOT NULL,
    last_updated_at TEXT
)
```

**In-memory tracking**:
```python
_dashboard_dirty: bool = False
_dashboard_last_edit: float = 0.0  # monotonic, for rate limiting
```

**Update triggers**: Turn start/complete/error in `_execute_turn()`, lifecycle events in notification handler. Rate-limited to 1 edit per 5 seconds.

**Files**:
| File | Action |
|------|--------|
| `handlers/dashboard.py` | **Create** — `DiscordDashboardMixin` |
| `state.py` | Modify — add `discord_dashboard` table |
| `config.py` | Modify — add `dashboard_channel_id: Optional[int]` |
| `rendering.py` | Modify — add `build_dashboard_embed()` |
| `service.py` | Modify — add mixin |
| `execution.py` | Modify — call `_mark_dashboard_dirty()` on turn start/complete |
| `notifications.py` | Modify — call `_mark_dashboard_dirty()` on lifecycle events |
| `constants.py` | Modify — add `DASHBOARD_MIN_EDIT_INTERVAL = 5.0` |

---

## 6. Bot Presence

**What**: Dynamic bot status reflecting swarm activity.

In `service.py`, add `_update_presence()`:
```python
async def _update_presence(self) -> None:
    active = len(self._turn_contexts)
    if active > 0:
        name = f"{active} task{'s' if active != 1 else ''}"
        activity = discord.Activity(type=discord.ActivityType.watching, name=name)
    else:
        activity = discord.Activity(type=discord.ActivityType.watching, name="for tasks")
    await self._bot.bot.change_presence(activity=activity)
```

Called on: turn start, turn complete (in `_execute_turn` finally block), and `on_ready`.

**Files**: `service.py`, `execution.py` (2 call sites).

---

## 7. Activity Feed

**What**: Per-workspace `#activity-feed` channels with rich embeds for every agent action.

Extend `DiscordNotificationHandlers` (`notifications.py`). At the bottom of each `_note_progress_*` method, additionally post to the activity feed:

| App-server event | Activity embed |
|-----------------|----------------|
| `item/commandExecution/requestApproval` | Yellow "Approval Requested" |
| `item/completed` (command) | Green "Command Executed" + output snippet |
| `item/completed` (file change) | Blue "Files Changed" + file list |
| `turn/completed` | Green "Turn Completed" / Red "Turn Failed" |
| `error` | Red "Error" |

Channel resolution: look up `activity` channel from `discord_scaffolded_channels` for the workspace. Fall back to thread parent.

**Files**: `notifications.py`, `rendering.py` (add `build_activity_embed()` variants).

---

## New Mixin Summary

| Mixin | File | Added to `service.py` |
|-------|------|-----------------------|
| `ScaffoldCommands` | `handlers/commands/scaffold.py` | Yes |
| `DiscordRBACMixin` | `handlers/rbac.py` | Yes |
| `DiscordAgentBusMixin` | `handlers/agent_bus.py` | Yes |
| `DiscordDashboardMixin` | `handlers/dashboard.py` | Yes |

Updated mixin list in `service.py:102`:
```python
class DiscordBotService(
    DiscordRuntimeHelpers,
    DiscordMessageTransport,
    DiscordNotificationHandlers,
    DiscordApprovalHandlers,
    DiscordQuestionHandlers,
    DiscordSelectionHandlers,
    DiscordCommandHandlers,
    SharedHelpers,
    ExecutionCommands,
    WorkspaceCommands,
    FormattingHelpers,
    ScaffoldCommands,          # NEW
    DiscordRBACMixin,          # NEW
    DiscordAgentBusMixin,      # NEW
    DiscordDashboardMixin,     # NEW
):
```

---

## State Schema Migration

Bump `DISCORD_SCHEMA_VERSION` from 1 to 2 in `state.py:25`. Add migration in `_ensure_schema()`:
- Check current version
- If v1: CREATE the 3 new tables, UPDATE version to 2
- All new tables use `IF NOT EXISTS` for safety

---

## Implementation Order

| Phase | Feature | Key files |
|-------|---------|-----------|
| 1 | Config + State schema | `config.py`, `state.py`, `constants.py` |
| 2 | RBAC mixin | `handlers/rbac.py`, `config.py`, `service.py` |
| 3 | Thread-per-task | `execution.py`, `messages.py`, `helpers.py` |
| 4 | Server scaffolding | `handlers/commands/scaffold.py`, `commands_runtime.py` |
| 5 | Dashboard + Presence | `handlers/dashboard.py`, `rendering.py`, `service.py` |
| 6 | Agent bus | `handlers/agent_bus.py`, `state.py`, `rendering.py` |
| 7 | Activity feed | `notifications.py`, `rendering.py` |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Channel creation rate limit (~10/10min) | Batch with delays in `/setup`, `asyncio.sleep(1)` between creates |
| Thread limit (1000 active/guild) | `auto_archive_duration=1440` ensures cleanup; completed threads archive |
| Webhook limit (15/channel) | Share webhooks; 3 agent identities is well under limit |
| Embed size (25 fields, 6000 chars) | Compact 1-line format for >15 workspaces; paginate if >25 |
| Lifecycle listener thread safety | Use `asyncio.run_coroutine_threadsafe()` to bridge to bot event loop |

---

## Verification

1. **Unit**: Run existing test suite to confirm no regressions
2. **Schema**: Start bot, verify SQLite has 3 new tables with correct schema
3. **`/setup`**: Run in test guild, verify categories + channels created, bindings set
4. **`/setup` idempotency**: Run again, verify no duplicates
5. **`/run` auto-thread**: Run a task, verify new thread created with correct name
6. **Thread lifecycle**: Verify thread renamed on completion/error
7. **RBAC**: Test with admin/dev/viewer roles, verify command access + denial messages
8. **Dashboard**: Verify pinned embed updates on turn start/complete
9. **Presence**: Verify bot status changes ("Watching 1 task" -> "Watching for tasks")
10. **Agent bus**: Trigger a lifecycle event, verify embed in `#agent-bus`
11. **Activity feed**: Run a task, verify action embeds in `#activity-feed`
