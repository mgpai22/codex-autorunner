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
12. [Multi-Agent Swarms (`/swarm`)](#12-multi-agent-swarms-swarm)
13. [Bot Presence](#13-bot-presence)
14. [Reconciliation / Self-Healing](#14-reconciliation--self-healing)
15. [State Store (Schema v3)](#15-state-store-schema-v3)
16. [Module Map](#16-module-map)
17. [Discord Platform Limits](#17-discord-platform-limits)
18. [Troubleshooting](#18-troubleshooting)

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
Four sections control the swarm surface: `scaffold`, `rbac`, `alerts`, and `swarm`.

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
      - swarm
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

### 2.4 Swarm Config

Controls the multi-agent swarm system (`/swarm` commands). See
[Section 12](#12-multi-agent-swarms-swarm) for full details.

```yaml
discord_bot:
  swarm:
    enabled: true
    max_agents: 6
    claude_binary: "claude"
    poll_interval_seconds: 0.5
    swarm_timeout_seconds: 7200.0
```

**Dataclass**: `SwarmConfig` in `config.py:178`

### 2.5 Optional Channel Overrides

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
| `/swarm` | yes | Start a multi-agent swarm |
| `/swarm-stop` | yes | Stop an active swarm |
| `/swarm-status` | yes | Show swarm status |

---

## 12. Multi-Agent Swarms (`/swarm`)

The swarm system spawns multiple Claude CLI agents that collaborate via a
filesystem-based teammate protocol. Each agent gets its own Discord forum
thread for observability. The protocol is modeled after
[claude-code-controller](https://github.com/The-Vibe-Company/claude-code-controller).

### 12.1 Architecture

```
Discord User
    │
    ▼  /swarm prompt:... preset:full-build
┌──────────────────┐
│  SwarmCommands    │  (slash command handler)
│  (mixin)         │
└────────┬─────────┘
         ▼
┌──────────────────┐
│  SwarmManager    │  (Discord bridge — forum threads, message routing, health)
└────────┬─────────┘
         ▼
┌──────────────────┐
│ SwarmController  │  (process management — spawn, message, shutdown, kill)
└────────┬─────────┘
         ▼
┌──────────────────┐
│    Protocol      │  (filesystem I/O — team config, inboxes, tasks)
│  ~/.claude/teams │
│  ~/.claude/tasks │
└────────┬─────────┘
         ▼
┌──────────────────┐
│  Claude CLI      │  N agents in --teammate-mode auto
│  (PTY wrapper)   │
└──────────────────┘
```

### 12.2 Teammate Protocol (Filesystem)

All agent coordination uses files under `~/.claude/`:

| Path | Purpose |
|---|---|
| `~/.claude/teams/{teamName}/config.json` | Team configuration (members, lead, description) |
| `~/.claude/teams/{teamName}/inboxes/{agentName}.json` | Per-agent inbox (messages array) |
| `~/.claude/tasks/{teamName}/{id}.json` | Per-task state file |

File operations use `fcntl.flock()` with 5 retries and 50–500ms exponential
backoff. All async I/O is bridged through a `ThreadPoolExecutor(max_workers=1)`,
matching the `state.py` pattern.

**Data structures** (matching claude-code-controller):

- **TeamConfig**: `{name, description, createdAt, leadAgentId, leadSessionId, members[]}`
- **TeamMember**: `{agentId, name, agentType, model, joinedAt, tmuxPaneId, cwd}`
- **InboxMessage**: `{from, text, timestamp, summary, read}`
- **TaskFile**: `{id, subject, description, activeForm, owner, status, blocks, blockedBy, metadata}`

**Environment variable**: `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is set on all
spawned agents.

**Implementation**: `swarm/protocol.py`

### 12.3 Process Management (SwarmController)

`SwarmController` manages agent processes:

- **Spawn**: Uses a Python PTY wrapper (`pty.fork()`) via `subprocess.Popen` with
  `os.setsid()` for process group cleanup. Command:
  ```
  claude --teammate-mode auto \
    --agent-id {name}@{team} --agent-name {name} \
    --team-name {team} --agent-type {type} \
    --model {model} --permission-mode bypassPermissions \
    --parent-session-id {uuid} -p "{prompt}"
  ```
- **Message**: Writes to agent inbox file, agent reads on next poll
- **Shutdown**: Writes `shutdown_request` to inbox → waits grace period → SIGTERM → SIGKILL
- **Polling**: Background `asyncio.Task` reads the controller's own inbox at
  configurable interval (default 0.5s)

**Implementation**: `swarm/controller.py`

### 12.4 Presets

Three built-in presets define agent compositions:

| Preset | Agents | Description |
|---|---|---|
| `code-review` | lead (opus), security-reviewer (sonnet), quality-reviewer (sonnet) | 3-agent code review |
| `full-build` | lead (opus), architect (opus), implementer (sonnet), tester (sonnet) | 4-agent full build |
| `research-deep` | lead (opus), researcher-1 (sonnet), researcher-2 (sonnet) | 3-agent deep research |

Custom presets can be defined in YAML config under `discord_bot.swarm.custom_presets`.
Custom presets take precedence over built-ins with the same name.

**Models**: `claude-opus-4-6` for leads/architects, `claude-sonnet-4-5-20250929`
for workers.

**Implementation**: `swarm/presets.py`

### 12.5 Commands

#### `/swarm prompt:<text> preset:<name>`

Start a multi-agent swarm.

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `prompt` | yes | — | The objective for the swarm |
| `preset` | no | `code-review` | Preset name (autocomplete from built-in + custom) |

**RBAC capability**: `can_run`

**Flow**:
1. RBAC check + verify swarm is enabled in config
2. Resolve workspace from channel binding
3. Find the tasks forum channel for this workspace from `discord_scaffolded_channels`
4. Initialize `SwarmManager` (lazy)
5. `SwarmManager.start_swarm()`:
   - Generate `swarm_id` (UUID) and `team_name` (`swarm-{id[:8]}`)
   - Create `SwarmController` → initialize filesystem dirs + config.json
   - For each role in the preset → create forum thread with agent card embed + `swarm` tag
   - Spawn each agent via PTY wrapper
   - Register message callback → start inbox polling + health monitor
   - Save swarm + agents to SQLite
6. Post summary embed with swarm status

#### `/swarm-stop [swarm_id]`

Stop an active swarm. If no `swarm_id` given, stops all active swarms.

**RBAC capability**: `can_stop`

**Shutdown flow**:
1. Mark swarm status → `stopping`
2. Send `shutdown_request` to each running agent's inbox
3. Wait `shutdown_grace_seconds` (default 10s)
4. Stop inbox polling + kill all remaining processes
5. Cancel health monitor task
6. Clean up filesystem (`~/.claude/teams/{team}/` + `~/.claude/tasks/{team}/`)
7. Mark swarm status → `stopped`

#### `/swarm-status [swarm_id]`

Show swarm status. If no `swarm_id` given, shows the most recent swarm for
the guild.

**Output**: Embed with swarm status, preset, creation time, and per-agent
status (name, role, model, status icon).

### 12.6 Discord Integration

- **Forum threads**: Each agent gets its own forum thread in the workspace's
  tasks forum, named `[swarm] {agent_name} — {swarm_id[:8]}`
- **Forum tag**: The `swarm` tag is applied to all swarm threads (added to the
  default `task_forum_tags` set)
- **Message routing**: Agent messages are polled from the controller inbox and
  posted to the agent's forum thread via `thread.send()`
- **Structured messages**: `idle_notification` and `shutdown_approved` messages
  update agent status in SQLite silently; `task_completed` messages extract the
  summary text
- **Stop button**: Swarm callbacks route `swarm:stop:{swarm_id}` custom IDs to
  `SwarmManager.stop_swarm()`

### 12.7 Health Monitoring

A background `asyncio.Task` per swarm checks health every
`health_check_interval_seconds` (default 5s):

| Check | Action |
|---|---|
| Swarm-level timeout exceeded (`swarm_timeout_seconds`, default 2h) | Stop the swarm |
| All agents have exited | Mark swarm `completed`, clean up session |
| Individual agent exited with code 0 | Mark agent `completed` |
| Individual agent exited with non-zero code | Mark agent `failed` |

### 12.8 Configuration

```yaml
discord_bot:
  swarm:
    enabled: true                          # Enable /swarm commands (default: true)
    max_agents: 6                          # Max agents per swarm (default: 6)
    default_lead_model: "claude-opus-4-6"
    default_worker_model: "claude-sonnet-4-5-20250929"
    claude_binary: "claude"                # Path to Claude CLI binary
    poll_interval_seconds: 0.5             # Inbox poll interval
    agent_timeout_seconds: 3600.0          # Per-agent timeout (1h)
    swarm_timeout_seconds: 7200.0          # Per-swarm timeout (2h)
    health_check_interval_seconds: 5.0     # Health check interval
    shutdown_grace_seconds: 10.0           # Grace period before SIGKILL
    custom_presets:                         # Custom preset definitions (optional)
      my-preset:
        description: "Custom 2-agent preset"
        roles:
          - name: lead
            model: claude-opus-4-6
            is_lead: true
            prompt_template: "You are the lead. {prompt}"
          - name: worker
            model: claude-sonnet-4-5-20250929
            prompt_template: "You are the worker. {prompt}"
```

**Dataclass**: `SwarmConfig` in `config.py:178`

### 12.9 Swarm Embeds

| Embed | Builder | Used By |
|---|---|---|
| Swarm summary | `build_swarm_summary_embed()` | `/swarm` response |
| Agent card | `build_swarm_agent_card_embed()` | Forum thread starter message |
| Swarm status | `build_swarm_status_embed()` | `/swarm-status` response |

All swarm embeds use `EMBED_COLOR_SWARM = 0x9B59B6` (purple).

---

## 13. Bot Presence

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

## 14. Reconciliation / Self-Healing

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
| Stale swarms (status="running") | Mark as `stopped` — Claude CLI processes don't survive bot restarts |

### Structured log events

| Event | When |
|---|---|
| `discord.reconcile.channel_recreated` | A scaffolded channel was found missing |
| `discord.reconcile.tag_recreated` | A forum tag was recreated |
| `discord.reconcile.dashboard_recreated` | Dashboard message was recreated |
| `discord.reconcile.guild_failed` | Guild-level reconcile error |
| `discord.swarm.stale_marked_stopped` | A running swarm was marked stopped after restart |

---

## 15. State Store (Schema v3)

Schema version bumped to 3 (`DISCORD_SCHEMA_VERSION = 3` in `state.py`).
Eight tables total. All use `CREATE TABLE IF NOT EXISTS` for safe migration.

### Tables

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

#### `discord_swarms`

Tracks swarm sessions (added in Schema v3).

```sql
CREATE TABLE IF NOT EXISTS discord_swarms (
    swarm_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    forum_channel_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    preset_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'starting',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT
);
```

**CRUD**: `save_swarm`, `get_swarm`, `list_swarms` (by guild/status), `update_swarm_status`, `delete_swarm`

#### `discord_swarm_agents`

Per-agent state within a swarm (added in Schema v3).

```sql
CREATE TABLE IF NOT EXISTS discord_swarm_agents (
    swarm_id TEXT NOT NULL REFERENCES discord_swarms(swarm_id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    model TEXT,
    is_lead INTEGER NOT NULL DEFAULT 0,
    discord_thread_id INTEGER,
    discord_root_message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'spawning',
    pid INTEGER,
    started_at TEXT,
    finished_at TEXT,
    last_message_at TEXT,
    PRIMARY KEY (swarm_id, agent_name)
);
```

**CRUD**: `save_swarm_agent`, `update_swarm_agent_status`, `list_swarm_agents`

### Async/sync bridge pattern

All state methods follow the existing pattern:
- Public async method (e.g., `save_task()`)
- Private sync method (e.g., `_save_task_sync()`)
- Bridged via `_run()` → `loop.run_in_executor(self._executor, ...)`

The single-threaded executor ensures SQLite thread-safety.

---

## 16. Module Map

### New files

| File | Lines | Description |
|---|---|---|
| `handlers/commands/scaffold.py` | ~550 | `ScaffoldCommands` mixin — `/setup` implementation |
| `handlers/commands/workspace_create.py` | ~750 | `WorkspaceCreateCommands` mixin — `/workspace create` and `/workspace clone` |
| `handlers/commands/swarm_commands.py` | ~245 | `SwarmCommands` mixin — `/swarm`, `/swarm-stop`, `/swarm-status` |
| `handlers/tasks.py` | ~740 | `DiscordTasksMixin` — task cards, controls, triage commands |
| `handlers/rbac.py` | ~165 | `DiscordRBACMixin` — role-based access control |
| `handlers/dashboard.py` | ~300 | `DiscordDashboardMixin` — live dashboard |
| `handlers/agent_bus.py` | ~310 | `DiscordAgentBusMixin` — agent bus + lifecycle bridge |
| `swarm/__init__.py` | 1 | Package init |
| `swarm/types.py` | ~97 | `SwarmAgentState`, `SwarmAgentRole`, `SwarmPreset`, `SwarmAgentInfo`, `SwarmSession` |
| `swarm/presets.py` | ~170 | `SWARM_PRESETS` dict, `get_preset()`, `list_presets()`, custom preset parsing |
| `swarm/protocol.py` | ~468 | Filesystem teammate protocol — team config, inbox, tasks, file locking |
| `swarm/controller.py` | ~368 | `SwarmController` — PTY spawn, message send, polling, shutdown, kill |
| `swarm/manager.py` | ~462 | `SwarmManager` — Discord bridge, forum threads, message routing, health monitoring |

### Modified files

| File | Changes |
|---|---|
| `config.py` | +`DiscordScaffoldConfig`, `DiscordRoleTier`, `DiscordRBACConfig`, `DiscordAlertConfig`, `SwarmConfig`; parsing in `from_raw()`; new fields on `DiscordBotConfig`; "swarm" added to default `task_forum_tags` |
| `state.py` | 8 tables in `_ensure_schema()`; full async/sync CRUD pairs; `DISCORD_SCHEMA_VERSION` → 3; swarm + swarm_agents tables |
| `service.py` | 6 new mixins in class hierarchy (incl. `SwarmCommands`); `_swarm_manager` attribute; swarm cleanup in `_shutdown()`; stale swarm recovery in `_reconcile_on_ready()` |
| `rendering.py` | 10 embed builders (added `build_swarm_summary_embed`, `build_swarm_agent_card_embed`, `build_swarm_status_embed`) |
| `notifications.py` | `_post_task_activity()`, `_maybe_send_task_alert()`, `_note_task_needs_approval()`, `_post_lifecycle_to_agent_bus()` |
| `handlers/commands/execution.py` | `/run` creates forum tasks when in scaffolded forum; state transitions wired (running, done, timeout, stopped, failed) |
| `handlers/commands_runtime.py` | `/setup`, `/workspace create`, `/workspace clone`, `/workspaces`, `/tasks list`, `/tasks mine`, `/swarm`, `/swarm-stop`, `/swarm-status` registered |
| `handlers/commands_spec.py` | Command specs: setup, workspace, workspaces, tasks, swarm, swarm-stop, swarm-status |
| `handlers/callbacks.py` | `task:*`, `wscreate:*`, and `swarm:*` custom_id routing |
| `helpers.py` | `sanitize_thread_name()` |
| `constants.py` | `THREAD_NAME_MAX_LEN = 100`; swarm constants (`SWARM_MAX_AGENTS`, `SWARM_POLL_INTERVAL_SECONDS`, etc.); `EMBED_COLOR_SWARM = 0x9B59B6` |

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
    WorkspaceCreateCommands,   # /workspace create, /workspace clone
    FormattingHelpers,
    ScaffoldCommands,          # /setup
    SwarmCommands,             # /swarm, /swarm-stop, /swarm-status
    DiscordRBACMixin,          # role-based access
    DiscordAgentBusMixin,      # agent bus + lifecycle bridge
    DiscordDashboardMixin,     # live dashboard
    DiscordTasksMixin,         # forum tasks + triage
):
```

---

## 17. Discord Platform Limits

Key limits relevant to the swarm surface:

| Limit | Value | Mitigation |
|---|---|---|
| Channel creation rate | ~10 per 10 min | `/setup` sleeps 0.5s between creates; reconcile handles gaps |
| Forum tags per channel | 20 | Default tag set is 12 (incl. "swarm"); leaves room for custom tags |
| Applied tags per thread | 5 | State tag + priority tag; older tags trimmed |
| Thread name length | 100 chars | `sanitize_thread_name()` truncates |
| Webhooks per channel | 15 | 3 agent identities well under limit |
| Embed description | 4096 chars | `_truncate()` applied to all embeds |
| Embed fields | 25 max | Dashboard/workspaces paginate if needed |
| Edit rate limit | ~5 per 5s per channel | Dashboard uses 2s debounce; progress uses 1.5s interval |
| Custom ID length | 100 chars | Format: `task:{action}:{guild_id}:{thread_id}` (fits) |
| Interaction deadline | 3 seconds | All handlers `defer()` immediately |

---

## 18. Troubleshooting

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

### `/swarm` says "No workspace bound to this channel"

The `/swarm` command requires the channel to have a workspace binding. Either:
- Run `/swarm` from a scaffolded `tasks` forum channel (auto-bound by `/setup`)
- Or use `/bind` to bind the current channel to a workspace first

### Swarm agents don't produce output in forum threads

Check:
1. Claude CLI is installed and accessible at the configured `claude_binary` path
2. The `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` env var is being set (check
   `SwarmController.spawn_agent()` logs)
3. Look for `discord.swarm.start_failed` or process spawn errors in logs
4. Verify `poll_interval_seconds` is reasonable (default 0.5s)

### Swarm shows "running" after bot restart

This is expected. On restart, `_reconcile_on_ready()` marks any swarms with
status `running` as `stopped`, since Claude CLI processes don't survive bot
restarts. Use `/swarm` to start a new swarm.

### Swarm times out

The default `swarm_timeout_seconds` is 7200s (2 hours). For long-running
swarms, increase this in config. Individual agents timeout at
`agent_timeout_seconds` (default 3600s / 1 hour).

---

## References

- `docs/discord/architecture.md` — Base bot architecture
- `docs/discord/discord-integration.md` — Dispatch, outbox, and trigger-mode specs
- `docs/discord/discord-swarm-surface-plan.md` — Original implementation plan
- `docs/discord/security.md` — Security considerations
- `docs/ops/discord-bot-runbook.md` — Operational runbook
