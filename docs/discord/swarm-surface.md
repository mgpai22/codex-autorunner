# Discord Swarm Control Surface

The swarm control surface turns a Discord server into the primary operational UI
for steering workspaces, tasks, approvals, and cross-agent coordination. Instead
of treating Discord as a thin chat surface, the server structure *is* the swarm
topology.

**Prerequisites**: The base Discord bot must be running (`car discord start`).
See `docs/discord/architecture.md` for setup and `docs/discord/discord-integration.md`
for the underlying dispatch/outbox/trigger-mode specifications.

---

## Table of Contents

1. [Mental Model](#1-mental-model)
2. [Configuration](#2-configuration)
3. [Server Scaffolding (`/setup`)](#3-server-scaffolding-setup)
4. [Forum-Backed Tasks](#4-forum-backed-tasks)
5. [Task Card & Persistent Controls](#5-task-card--persistent-controls)
6. [Role-Based Access Control (RBAC)](#6-role-based-access-control-rbac)
7. [Live Dashboard](#7-live-dashboard)
8. [Agent Communication Bus](#8-agent-communication-bus)
9. [Activity Feed & Notifications](#9-activity-feed--notifications)
10. [Alerts (Role Pings)](#10-alerts-role-pings)
11. [Triage & Navigation Commands](#11-triage--navigation-commands)
12. [Bot Presence](#12-bot-presence)
13. [Reconciliation / Self-Healing](#13-reconciliation--self-healing)
14. [State Store (Schema v2)](#14-state-store-schema-v2)
15. [Module Map](#15-module-map)
16. [Discord Platform Limits](#16-discord-platform-limits)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Mental Model

Each Discord primitive maps to a swarm concept:

| Discord Primitive | Swarm Concept |
|---|---|
| Category | Workspace namespace |
| Forum channel (`tasks`) | Workspace task backlog (triage-friendly, mobile-friendly) |
| Forum thread/post | One task execution context (`topic_key`) |
| Forum tags | Task state + priority (`running`, `needs-approval`, `p0`, etc.) |
| Text channels (`activity-feed`, `approvals`) | Per-workspace observability + approval handling |
| Control Plane category | Cross-workspace dashboard + agent bus + notifications |
| Webhooks | Multi-identity posting (PMA / Codex Agent / Autorunner) |
| Persistent Views (buttons/selects) | Actionable UI without leaving Discord |
| Bot presence | Real-time swarm activity indicator ("Watching N tasks") |

Target server structure after `/setup`:

```
[Category: frontend-app]
  tasks              (Forum)   -- Bound to workspace. /run creates a post/thread.
  #activity-feed               -- Read-only. Rich embeds for every agent action.
  #approvals                   -- Approval requests with buttons (+ optional role ping).

[Category: backend-api]
  tasks              (Forum)
  #activity-feed
  #approvals

[Category: Control Plane]
  #dashboard                   -- Pinned embed: real-time workspace status grid.
  #agent-bus                   -- PMA decisions, cross-workspace handoffs, lifecycle.
  #notifications               -- General lifecycle events.
```

---

## 2. Configuration

All swarm surface config lives under `discord_bot` in `codex-autorunner.yml`.
Three new sections were added: `scaffold`, `rbac`, and `alerts`.

### 2.1 Scaffold Config

Controls the `/setup` command behavior and channel naming.

```yaml
discord_bot:
  scaffold:
    enabled: true                    # Enable /setup command (default: false)
    auto_bind: true                  # Auto-bind tasks forums to workspaces (default: true)
    category_prefix: ""              # Optional prefix for category names (e.g., "CAR-")
    tasks_channel_kind: "forum"      # "forum" (default) or "text"
    tasks_channel_name: "tasks"      # Name for the tasks channel
    task_forum_tags:                 # Tags created on each tasks forum
      - queued
      - running
      - needs-approval
      - blocked
      - done
      - failed
      - timeout
      - stopped
      - p0
      - p1
      - p2
    activity_channel_name: "activity-feed"
    approval_channel_name: "approvals"
    dashboard_channel_name: "dashboard"
    agent_bus_channel_name: "agent-bus"
    notifications_channel_name: "notifications"
```

**Dataclass**: `DiscordScaffoldConfig` in `config.py:129`

### 2.2 RBAC Config

Maps Discord roles to permission tiers.

```yaml
discord_bot:
  rbac:
    enabled: true                          # Enable RBAC checks (default: false)
    use_default_member_permissions: true    # Set Discord command permissions (default: true)
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
    default_tier: viewer                   # Tier for users matching no role (default: "viewer")
```

**Dataclasses**: `DiscordRBACConfig` (`config.py:168`), `DiscordRoleTier` (`config.py:156`)

### 2.3 Alerts Config

Controls role pings for urgent events.

```yaml
discord_bot:
  alerts:
    enabled: true                          # Enable alert pings (default: true)
    alert_role_id: 111222333               # Discord role ID to ping (null = disabled)
    per_task_cooldown_seconds: 900         # Min seconds between pings for same task+type (default: 900)
    global_cooldown_seconds: 10            # Min seconds between any pings (default: 10)
```

**Dataclass**: `DiscordAlertConfig` (`config.py:176`)

### 2.4 Optional Channel Overrides

For pre-existing channels (skip `/setup`):

```yaml
discord_bot:
  dashboard_channel_id: 444555666          # Pre-existing dashboard channel
  agent_bus_channel_id: 777888999          # Pre-existing agent-bus channel
```

---

## 3. Server Scaffolding (`/setup`)

**Command**: `/setup`
**Permission**: Requires `administrator` by default (`default_member_permissions`)
**RBAC capability**: `can_setup`
**Mixin**: `ScaffoldCommands` (`handlers/commands/scaffold.py`)

### What it does

1. Lists all workspaces from the hub supervisor
2. For each workspace, creates a **Category** and three channels:
   - `tasks` — Forum channel with tags (queued, running, done, failed, etc.)
   - `activity-feed` — Read-only text channel for state update embeds
   - `approvals` — Text channel for approval request buttons
3. Creates a **Control Plane** category with:
   - `#dashboard` — Read-only, pinned status embed
   - `#agent-bus` — Read-only, structured coordination embeds via webhooks
   - `#notifications` — General lifecycle events

### Channel permissions

Read-only channels (`activity-feed`, `dashboard`, `agent-bus`) are created with
permission overwrites:
- `@everyone`: `send_messages=False`
- Bot: `send_messages=True`

### Idempotency

`/setup` is fully idempotent:
- Before creating any channel, it checks `discord_scaffolded_channels` in SQLite
  and verifies the channel still exists via `bot.fetch_channel()`
- If a channel was manually deleted, it recreates and updates the stored ID
- Missing forum tags are added to existing forum channels without duplicating

### Auto-binding

When `scaffold.auto_bind` is `true` (default), each `tasks` forum is
automatically bound to its workspace path via `set_channel_binding()`. This means
`/run` inside those forums immediately resolves the correct workspace.

### Rate limiting

Channel creation is rate-limited by Discord (~10/10min). The scaffold sleeps
0.5s between creates (`_SCAFFOLD_SLEEP_SECONDS`).

### Output

A summary embed is posted showing:
- Number of workspaces scaffolded
- Channels created (by type)
- Forum tags created

---

## 4. Forum-Backed Tasks

Every task is a **forum post/thread** in the workspace's `tasks` forum. The
thread serves as the isolated execution context (`topic_key`).

### Creation flow

When `/run <prompt> [model] [effort]` is used inside a scaffolded `tasks` forum:

1. **Resolve workspace** from the channel binding
2. **Resolve the forum channel** from `discord_scaffolded_channels`
3. **Create forum thread** via `ForumChannel.create_thread()`:
   - Name: `task-{prompt[:80]}` sanitized (max 100 chars, fallback: `task-{timestamp}`)
   - Starter message: **Task Card** embed (see next section)
   - Initial tag: `queued`
4. **Build topic_key**: `{guild_id}:{forum_channel_id}:{thread.id}`
5. **Store in SQLite**: `discord_tasks` table with root_message_id, prompt, user
6. **Apply model/effort**: set `model` and `reasoning_effort` on the topic record (per-invocation override)
7. **Execute the turn** in the thread context

### Inline model & effort

`/run` accepts optional `model` and `effort` parameters so each invocation can
override the model and reasoning effort without changing persistent topic state
via `/model` or `/agent`:

```
/run fix the bug                                  # defaults: gpt-5.3-codex, medium
/run fix the bug model:claude-sonnet-4-5-20250929   # override model only
/run fix the bug effort:high                      # override effort only
/run fix the bug model:o4-mini effort:xhigh       # override both
```

| Parameter | Default | Autocomplete | Valid values |
|-----------|---------|--------------|--------------|
| `model` | `gpt-5.3-codex` | Live model list from app-server (falls back to well-known models) | Any model ID returned by the app-server |
| `effort` | `medium` | Static list | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |

The override is applied to the `DiscordTopicRecord` before execution, so it
affects the current turn. Subsequent `/run` calls without explicit values revert
to the defaults. The existing `/model` and `/agent` commands continue to work
independently for persistent configuration.

**Implementation**: `ExecutionCommands._cmd_run_impl()` in `handlers/commands/execution.py`

### State transitions (forum tags)

Tags serve as the primary visual state indicator:

```
queued → running        (when execution starts)
running → done          (on success)
running → failed        (on error)
running → timeout       (on timeout)
running → needs-approval (when approval requested)
any → stopped           (on /stop or Stop button)
```

When a state transition occurs, `_update_task_state()`:
1. Updates `discord_tasks.last_state` in SQLite
2. Replaces state tags on the forum thread (removes old state tags, applies new one)
3. Posts to the activity feed channel
4. Refreshes the Task Card embed

### Thread naming

`sanitize_thread_name()` (`helpers.py`) ensures thread names are Discord-safe:
- Max 100 characters (`THREAD_NAME_MAX_LEN` in `constants.py`)
- Stripped of invalid characters for Discord thread names

**Key file**: `handlers/tasks.py` — `DiscordTasksMixin._create_forum_task()`

---

## 5. Task Card & Persistent Controls

The **Task Card** is the starter message in every forum task thread. It acts as
the stable, always-visible UI surface for that task.

### Task Card embed fields

| Field | Content |
|---|---|
| Title | `Task • {workspace_name}` |
| Description | The original prompt text |
| Workspace | Workspace name/path |
| Initiator | Discord user mention |
| Status | Current state (queued, running, done, etc.) |
| Approvals | Mode, approval policy, sandbox policy |
| Created | ISO timestamp |
| Updated | ISO timestamp (last state change) |
| Links | Jump links to agent response and latest activity |

Color-coded by status: blue (info/queued), yellow (running), green (done),
orange (warning/needs-approval/blocked/stopped), red (failed/timeout).

**Builder**: `build_task_card_embed()` in `rendering.py:245`

### Persistent button controls

The Task Card has four buttons that survive bot restarts:

| Button | Action | Style |
|---|---|---|
| **Stop** | Interrupts the active turn | Danger (red) |
| **Set approvals** | Opens ephemeral select menu (safe/yolo) | Secondary (grey) |
| **Rerun** | Re-runs `initial_prompt` with current settings | Primary (blue) |
| **Escalate (p0)** | Applies `p0` tag + pings `alert_role_id` | Secondary (grey) |

**Custom ID format**: `task:{action}:{guild_id}:{thread_id}`

### Button callbacks

- **Stop**: Calls `_interrupt_turn()`, updates state to `stopped`
- **Set approvals**: Shows `TaskApprovalsView` (ephemeral Select with safe/yolo),
  updates the topic's `approval_mode` and refreshes the Task Card
- **Rerun**: Reads `initial_prompt` from `discord_tasks`, resolves workspace
  binding, creates a new `DiscordTopicRecord` if needed, sets state to `queued`,
  and spawns `_execute_turn()`
- **Escalate**: Applies `p0` forum tag (additive, doesn't remove state tag),
  pings `alert_role_id` with cooldown enforcement

### Persistence across restarts

On `on_ready()`, `_reregister_task_card_views()` iterates all tasks in
`discord_tasks` and calls `bot.add_view(TaskCardView(...))` for each, using a
`_task_views_registered` set to avoid duplicates.

**View classes**: `TaskCardView`, `TaskApprovalsView` in `handlers/tasks.py:38-85`

---

## 6. Role-Based Access Control (RBAC)

RBAC maps Discord roles to permission tiers that control which commands a user
can execute and what approval mode is effective.

**Mixin**: `DiscordRBACMixin` (`handlers/rbac.py`)

### Enforcement layers

1. **Allowlist** (first pass): guild/channel/role/user dimensions — unchanged
2. **RBAC** (second pass): per-command capability check within allowed contexts

### How tier matching works

`_get_user_tier(user_roles)`:
1. Extract the user's Discord role IDs from the interaction
2. Iterate tiers in order (highest privilege first)
3. First tier where user has a matching `role_id` wins
4. If no match, fall back to `default_tier` (default: `viewer`)

### Capabilities

Each tier defines boolean capabilities:

| Capability | Controls |
|---|---|
| `can_setup` | `/setup` command access |
| `can_run` | `/run` command access |
| `can_stop` | `/stop` command and Stop button |
| `can_bind` | `/bind` command access |
| `read_only` | View-only access (no commands) |

### Approval mode resolution

The effective approval mode follows a priority chain:

```
topic override > RBAC tier > config default
```

`_resolve_approval_mode_for_user(interaction)` returns the tier's
`approval_mode` (e.g., `safe` or `yolo`), which is used when no topic-level
override exists.

### Native command permissions

`/setup` is registered with `default_member_permissions` set to administrator.
This is defense-in-depth only — RBAC checks are authoritative.

---

## 7. Live Dashboard

A pinned embed in `#dashboard` showing real-time status of all workspaces.

**Mixin**: `DiscordDashboardMixin` (`handlers/dashboard.py`)

### Dashboard embed

Each workspace is shown as an embed field with:
- Status icon (running/done/failed/paused)
- Active task count
- Current status string
- Last activity timestamp

**Builder**: `build_dashboard_embed()` in `rendering.py:477`

### Update triggers

The dashboard is refreshed when:
- A turn starts or completes
- A task state changes
- A lifecycle event fires
- Bot starts up (via reconciliation)

### Debounced refresh

`_mark_dashboard_dirty(guild_id)` schedules a refresh with a 2-second debounce
(`_DASHBOARD_DEBOUNCE_SECONDS`). Multiple rapid events coalesce into a single
edit, respecting Discord's edit rate limit.

### Channel resolution

The dashboard channel is resolved in order:
1. `config.dashboard_channel_id` (explicit override)
2. `discord_scaffolded_channels` (from `/setup`)
3. Fallback: scan guild channels for one named "dashboard"

### Persistence

Dashboard state is stored in `discord_dashboard`:
- `guild_id` (PK), `channel_id`, `message_id`, `pinned_at`, `last_updated_at`

The pinned message is recreated if manually deleted (detected during reconcile).

---

## 8. Agent Communication Bus

The `#agent-bus` channel shows cross-agent coordination as structured embeds,
with distinct identities per agent via webhooks.

**Mixin**: `DiscordAgentBusMixin` (`handlers/agent_bus.py`)

### Agent identities

| Agent Name | Role | Posts As |
|---|---|---|
| **Project Manager** | PMA decisions, orchestration moves, handoffs | Own webhook |
| **Codex Agent** | Task updates | Own webhook |
| **Autorunner** | Lifecycle events (flow paused/completed/failed) | Own webhook |

Each agent posts via its own Discord webhook with a distinct `username`. Webhooks
are created lazily and cached in memory + SQLite (`discord_agent_webhooks`).

### Lifecycle event bridge

On `on_ready()`, `_register_lifecycle_listener()` registers a callback with the
hub's `LifecycleEventEmitter`. Since lifecycle events fire from sync threads,
the callback uses `asyncio.run_coroutine_threadsafe()` to bridge into the bot's
event loop.

### Event types

| Event | Posted By | Content |
|---|---|---|
| `DISPATCH_CREATED` | Project Manager | PMA dispatch details |
| `flow_paused` | Autorunner | Flow pause notification |
| `flow_completed` | Autorunner | Flow completion |
| `flow_failed` | Autorunner | Flow failure with error |
| Cross-workspace handoff | Project Manager | Source → Target with reason |

### Handoff detection

When lifecycle event data contains `source_workspace` and `target_workspace`
keys, a handoff embed is posted:

```
[Handoff] frontend-app → backend-api
"API endpoint needs updating to match new frontend schema"
```

**Builder**: `build_handoff_embed()` in `rendering.py:571`

### Webhook management

- Webhooks are created per `(guild_id, channel_id, agent_name)`
- Stored in `discord_agent_webhooks` table
- In-memory cache (`_agent_webhook_cache`) avoids repeated DB lookups
- Fallback: if webhook send fails, posts directly as the bot

---

## 9. Activity Feed & Notifications

Per-workspace `#activity-feed` channels receive rich embeds for every task state
change.

### Activity feed posts

When `_post_task_activity()` fires (called by `_update_task_state()`):

1. Looks up the task from `discord_tasks`
2. Resolves the workspace's `activity` channel from `discord_scaffolded_channels`
3. Posts a color-coded embed with:
   - Thread link (clickable `<#thread_id>`)
   - Initiator mention
   - Current state
   - Prompt preview (first 200 chars)
4. Stores the activity message ID in `discord_tasks.last_activity_message_id`
5. The Task Card is updated with a "Latest activity" jump link

### Color coding

| State | Color |
|---|---|
| running | Blue (progress) |
| done | Green (success) |
| failed, timeout | Red (error) |
| needs-approval, blocked, stopped | Orange (warning) |
| queued, other | Grey (info) |

### Approval state bridge

When the app-server requests an approval (`item/commandExecution/requestApproval`
or `item/fileChange/requestApproval`), `_note_task_needs_approval()` automatically
updates the forum task tag to `needs-approval`.

### Lifecycle events to agent bus

Lifecycle notifications (`flow_paused`, `flow_completed`, `flow_failed`) are also
forwarded to the agent bus via `_post_lifecycle_to_agent_bus()`.

---

## 10. Alerts (Role Pings)

Optional role pings for urgent events with deduplication.

### Trigger events

| Event | Alert Type | Message |
|---|---|---|
| Task failed | `failed` | `@role Task **failed**.` |
| Task timed out | `timeout` | `@role Task **timed out**.` |
| Escalation (p0 button) | `p0` | `@role Escalated to **p0**.` |

### Deduplication

Alerts are deduped using the `discord_alerts` table:

- **Per-task cooldown**: `per_task_cooldown_seconds` (default: 900s / 15min).
  Same `(guild_id, thread_id, alert_type)` won't fire again within the cooldown.
- **Global cooldown**: `global_cooldown_seconds` (default: 10s). Prevents
  burst pings across tasks.

`should_alert()` checks the `last_sent_at` timestamp against the cooldown before
allowing a ping.

### Disabling alerts

Set `alerts.alert_role_id: null` or `alerts.enabled: false` to disable all pings.

---

## 11. Triage & Navigation Commands

Discord-native navigation commands so the swarm is operable from mobile.

### `/workspace create`

Interactive multi-step flow: select type (new / clone / worktree) → fill modal → confirm → scaffold.

Steps:
1. Select menu with workspace type
2. Modal collects type-specific fields (repo ID, git URL, branch, etc.)
3. Bot defers, creates workspace via `HubSupervisor`, scaffolds category + channels, auto-binds

**Implementation**: `WorkspaceCreateCommands` mixin in `handlers/commands/workspace_create.py`
**Session**: `WorkspaceCreateSession` dataclass in `types.py`
**Pipeline**: `_ws_create_execute()` — shared by both `/workspace create` and `/workspace clone`

### `/workspace clone`

Direct slash command shortcut for cloning a git repository into a new workspace. No modal — just type the URL:

```
/workspace clone url:github.com/user/repo
/workspace clone url:https://www.github.com/user/repo.git
/workspace clone url:git@github.com:user/repo.git name:my-project
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `url` | yes | Git URL to clone (bare domain, HTTPS, SSH, `git://`) |
| `name` | no | Custom workspace name (inferred from URL if omitted) |

**URL normalization** (`normalize_git_url()`):

| Input | Normalized Output |
|-------|-------------------|
| `https://github.com/user/repo` | `https://github.com/user/repo` |
| `https://www.github.com/user/repo` | `https://github.com/user/repo` |
| `github.com/user/repo` | `https://github.com/user/repo` |
| `www.github.com/user/repo` | `https://github.com/user/repo` |
| `github.com/user/repo.git` | `https://github.com/user/repo.git` |
| `git@github.com:user/repo.git` | `git@github.com:user/repo.git` |
| `git://github.com/user/repo` | `git://github.com/user/repo` |
| `gitlab.example.com/org/repo` | `https://gitlab.example.com/org/repo` |

**Validation** (`_validate_git_url()`): rejects empty URLs, SSH with no path, scheme with no hostname/path.

**Flow**: RBAC check (`can_setup`) → defer ephemeral → normalize → validate → create `WorkspaceCreateSession` → `_ws_create_execute()` (clone → scaffold category + channels → auto-bind → confirmation embed).

**Implementation**: `WorkspaceCreateCommands._cmd_workspace_clone_impl()` in `handlers/commands/workspace_create.py`

### `/workspaces`

Lists all scaffolded workspaces with clickable channel links.

Each workspace shows:
- Tasks channel: `<#channel_id>`
- Activity feed: `<#channel_id>`
- Approvals: `<#channel_id>`

**Implementation**: `DiscordTasksMixin._cmd_workspaces_impl()` in `handlers/tasks.py:723`
**Embed**: `build_workspaces_embed()` in `rendering.py:422`

### `/tasks list [workspace] [tag]`

Lists tasks filtered by workspace and/or tag/state. Backed by SQLite queries
against `discord_tasks` — does not scan Discord history.

Each task entry shows:
- Thread reference (`<#thread_id>` — clickable on mobile)
- Current state (bold)
- Prompt preview (first 90 chars)

**Implementation**: `DiscordTasksMixin._cmd_tasks_list_impl()` in `handlers/tasks.py:702`
**Embed**: `build_tasks_list_embed()` in `rendering.py:379`

### `/tasks mine`

Shows tasks created by the calling user. Uses `created_by_user_id` filter.

**Implementation**: `DiscordTasksMixin._cmd_tasks_mine_impl()` in `handlers/tasks.py:713`

### Command specs

All new commands are registered in `commands_spec.py`:

| Command | `allow_during_turn` | Description |
|---|---|---|
| `/setup` | yes | Scaffold swarm control surface channels |
| `/workspace` | yes | Workspace management (create, clone) |
| `/workspaces` | yes | List scaffolded workspaces |
| `/tasks` | yes | Task triage and navigation |

---

## 12. Bot Presence

Dynamic bot status reflecting swarm activity.

```
Watching 0 tasks
Watching 1 task
Watching 3 tasks
```

`_update_presence()` in `service.py:371` sets the bot's Discord presence based
on the number of active turns (`len(self._turn_contexts)`).

Called on:
- `on_ready()`
- Turn start
- Turn completion

---

## 13. Reconciliation / Self-Healing

On `on_ready()`, the bot runs a reconciliation pass to ensure the control surface
is intact after restarts, manual edits, or partial failures.

**Flow**: `_reconcile_on_ready()` → `_reconcile_guild()` (per guild)

### What gets reconciled

| Check | Action if missing |
|---|---|
| Scaffolded channels exist | Log `discord.reconcile.channel_recreated` (does not auto-recreate; run `/setup` again) |
| Forum tags on tasks forums | Recreate missing tags via `_ensure_forum_tags()` |
| Dashboard pinned message | Recreate via `_ensure_dashboard()` |
| Task Card persistent views | Re-register via `_reregister_task_card_views()` |

### Structured log events

| Event | When |
|---|---|
| `discord.reconcile.channel_recreated` | A scaffolded channel was found missing |
| `discord.reconcile.tag_recreated` | A forum tag was recreated |
| `discord.reconcile.dashboard_recreated` | Dashboard message was recreated |
| `discord.reconcile.guild_failed` | Guild-level reconcile error |

---

## 14. State Store (Schema v2)

Schema version bumped from 1 to 2 (`DISCORD_SCHEMA_VERSION = 2` in `state.py`).
Six new tables were added. All use `CREATE TABLE IF NOT EXISTS` for safe
migration.

### New tables

#### `discord_scaffolded_channels`

Tracks channels created by `/setup`.

```sql
CREATE TABLE IF NOT EXISTS discord_scaffolded_channels (
    guild_id INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,  -- 'category'|'tasks'|'activity'|'approvals'|'dashboard'|'agent_bus'|'notifications'
    discord_channel_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, workspace_id, channel_type)
);
```

**CRUD**: `save_scaffolded_channel`, `get_scaffolded_channel`, `list_scaffolded_channels`, `delete_scaffolded_channel`

#### `discord_forum_tags`

Maps tag names to Discord tag IDs per workspace forum.

```sql
CREATE TABLE IF NOT EXISTS discord_forum_tags (
    guild_id INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    forum_channel_id INTEGER NOT NULL,
    tag_name TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, workspace_id, tag_name)
);
```

**CRUD**: `save_forum_tag`, `get_forum_tags`, `delete_forum_tag`

#### `discord_tasks`

Per-task state for forum-backed tasks.

```sql
CREATE TABLE IF NOT EXISTS discord_tasks (
    guild_id INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    forum_channel_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    root_message_id INTEGER NOT NULL,
    created_by_user_id INTEGER,
    initial_prompt TEXT,
    created_at TEXT NOT NULL,
    last_state TEXT,
    last_state_at TEXT,
    last_activity_message_id INTEGER,
    PRIMARY KEY (guild_id, thread_id)
);
```

**CRUD**: `save_task`, `get_task`, `list_tasks` (with filters), `update_task_state`, `update_task_activity`

#### `discord_alerts`

Tracks when alerts were last sent for deduplication.

```sql
CREATE TABLE IF NOT EXISTS discord_alerts (
    guild_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    alert_type TEXT NOT NULL,   -- 'needs-approval'|'failed'|'timeout'|'p0'
    last_sent_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, thread_id, alert_type)
);
```

**CRUD**: `save_alert`, `get_alert`, `should_alert`

#### `discord_agent_webhooks`

Per-agent webhook credentials for the agent bus.

```sql
CREATE TABLE IF NOT EXISTS discord_agent_webhooks (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    webhook_id INTEGER NOT NULL,
    webhook_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id, agent_name)
);
```

**CRUD**: `save_webhook`, `get_webhook`, `list_webhooks`

#### `discord_dashboard`

Dashboard pinned message tracking.

```sql
CREATE TABLE IF NOT EXISTS discord_dashboard (
    guild_id INTEGER NOT NULL PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    pinned_at TEXT NOT NULL,
    last_updated_at TEXT
);
```

**CRUD**: `save_dashboard`, `get_dashboard`, `update_dashboard_timestamp`

### Async/sync bridge pattern

All state methods follow the existing pattern:
- Public async method (e.g., `save_task()`)
- Private sync method (e.g., `_save_task_sync()`)
- Bridged via `_run()` → `loop.run_in_executor(self._executor, ...)`

The single-threaded executor ensures SQLite thread-safety.

---

## 15. Module Map

### New files

| File | Lines | Description |
|---|---|---|
| `handlers/commands/scaffold.py` | ~550 | `ScaffoldCommands` mixin — `/setup` implementation |
| `handlers/commands/workspace_create.py` | ~750 | `WorkspaceCreateCommands` mixin — `/workspace create` and `/workspace clone` |
| `handlers/tasks.py` | ~740 | `DiscordTasksMixin` — task cards, controls, triage commands |
| `handlers/rbac.py` | ~165 | `DiscordRBACMixin` — role-based access control |
| `handlers/dashboard.py` | ~300 | `DiscordDashboardMixin` — live dashboard |
| `handlers/agent_bus.py` | ~310 | `DiscordAgentBusMixin` — agent bus + lifecycle bridge |

### Modified files

| File | Changes |
|---|---|
| `config.py` | +`DiscordScaffoldConfig`, `DiscordRoleTier`, `DiscordRBACConfig`, `DiscordAlertConfig`; parsing in `from_raw()`; new fields on `DiscordBotConfig` |
| `state.py` | 6 new tables in `_ensure_schema()`; full async/sync CRUD pairs; `DISCORD_SCHEMA_VERSION` → 2 |
| `service.py` | 5 new mixins in class hierarchy; `_update_presence()`, `_reconcile_on_ready()`, `_reconcile_guild()`, `_reconcile_dashboard()`, `_reregister_task_card_views()`; lifecycle listener registration in `on_ready()` |
| `rendering.py` | 7 new embed builders: `build_setup_summary_embed`, `build_task_card_embed`, `build_tasks_list_embed`, `build_workspaces_embed`, `build_dashboard_embed`, `build_agent_bus_embed`, `build_handoff_embed` |
| `notifications.py` | `_post_task_activity()`, `_maybe_send_task_alert()`, `_note_task_needs_approval()`, `_post_lifecycle_to_agent_bus()` |
| `handlers/commands/execution.py` | `/run` creates forum tasks when in scaffolded forum; state transitions wired (running, done, timeout, stopped, failed) |
| `handlers/commands_runtime.py` | `/setup`, `/workspace create`, `/workspace clone`, `/workspaces`, `/tasks list`, `/tasks mine` registered |
| `handlers/commands_spec.py` | New command specs (setup, workspace, workspaces, tasks) |
| `handlers/callbacks.py` | `task:*` and `wscreate:*` custom_id routing |
| `helpers.py` | `sanitize_thread_name()` |
| `constants.py` | `THREAD_NAME_MAX_LEN = 100` |

### Updated mixin list

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
    WorkspaceCreateCommands,   # NEW — /workspace create, /workspace clone
    FormattingHelpers,
    ScaffoldCommands,          # NEW — /setup
    DiscordRBACMixin,          # NEW — role-based access
    DiscordAgentBusMixin,      # NEW — agent bus + lifecycle bridge
    DiscordDashboardMixin,     # NEW — live dashboard
    DiscordTasksMixin,         # NEW — forum tasks + triage
):
```

---

## 16. Discord Platform Limits

Key limits relevant to the swarm surface:

| Limit | Value | Mitigation |
|---|---|---|
| Channel creation rate | ~10 per 10 min | `/setup` sleeps 0.5s between creates; reconcile handles gaps |
| Forum tags per channel | 20 | Default tag set is 11; leaves room for custom tags |
| Applied tags per thread | 5 | State tag + priority tag; older tags trimmed |
| Thread name length | 100 chars | `sanitize_thread_name()` truncates |
| Webhooks per channel | 15 | 3 agent identities well under limit |
| Embed description | 4096 chars | `_truncate()` applied to all embeds |
| Embed fields | 25 max | Dashboard/workspaces paginate if needed |
| Edit rate limit | ~5 per 5s per channel | Dashboard uses 2s debounce; progress uses 1.5s interval |
| Custom ID length | 100 chars | Format: `task:{action}:{guild_id}:{thread_id}` (fits) |
| Interaction deadline | 3 seconds | All handlers `defer()` immediately |

---

## 17. Troubleshooting

### `/setup` creates duplicate categories

Run `/setup` again — it's idempotent. If channels were manually deleted, new
ones will be created and the old IDs replaced in SQLite.

### Forum tags are missing

Reconciliation on restart recreates missing tags. You can also re-run `/setup`.

### Task Card buttons don't work after restart

Check logs for `discord.task.view_register_failed`. The bot re-registers all
persistent views on `on_ready()`. If tasks are missing from `discord_tasks`,
the views won't be restored.

### Dashboard embed not updating

Check that `dashboard_channel_id` is set or that `/setup` created the channel.
Look for `discord.dashboard.ensure_failed` in logs. The dashboard uses a 2s
debounce — rapid events may appear delayed.

### Agent bus not showing events

Verify:
1. The hub supervisor has a `lifecycle_emitter` (`_register_lifecycle_listener()`)
2. The `#agent-bus` channel exists (check `discord_scaffolded_channels`)
3. Look for `discord.agent_bus.webhook.ensure_failed` or
   `discord.agent_bus.post_failed` in logs

### Alerts not pinging

Check:
1. `alerts.enabled: true` and `alerts.alert_role_id` is set
2. The role is mentionable in Discord
3. Cooldown hasn't been reached (`should_alert()` checks `discord_alerts` table)

### RBAC denying commands unexpectedly

Enable debug logging and look for `discord.rbac.denied` events. These include
the `capability`, `tier`, `guild_id`, and `user_id` to identify the mismatch.

---

## References

- `docs/discord/architecture.md` — Base bot architecture
- `docs/discord/discord-integration.md` — Dispatch, outbox, and trigger-mode specs
- `docs/discord/discord-swarm-surface-plan.md` — Original implementation plan
- `docs/discord/security.md` — Security considerations
- `docs/ops/discord-bot-runbook.md` — Operational runbook
