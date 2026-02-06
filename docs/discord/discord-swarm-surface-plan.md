# Discord-Native Agent Swarm Control Surface

## Context

The Discord bot currently treats Discord as a thin chat surface: users manually
`/bind` channels, manually create threads, and rely on a single notification
channel. Discord already provides the primitives we want for a swarm control
surface (categories, forum channels, threads, roles/permissions, persistent UI
components, webhooks, presence, searchable history, and a strong mobile app).

This plan turns the Discord server structure into the swarm topology. The goal
is that a Discord guild is not just “where messages happen”, but the primary,
operational UI for steering workspaces, tasks, approvals, and coordination.

## Key Decisions

- **Tasks are forum-backed**: each workspace has a `tasks` **Forum channel** and
  each task is a **forum post/thread** with tags.
- **Urgent events ping a role**: approval requests and turn failures/timeouts can
  ping an `alert_role_id`, with dedupe + rate limiting.
- **All tasks are public**: no private/sensitive task mode in this surface.

## Mental Model (Discord Primitive -> Swarm Concept)

- Category -> Workspace namespace
- Forum channel (`tasks`) -> Workspace task backlog (triage-friendly, mobile-friendly)
- Forum thread -> One task execution context (`topic_key`)
- Forum tags -> Task state + priority (`running`, `needs-approval`, `p0`, etc.)
- Text channels (`activity-feed`, `approvals`) -> Per-workspace observability + approvals
- Control plane category -> Cross-workspace dashboard + bus + notifications
- Webhooks -> Multi-identity posting (PMA/Codex/System)
- Persistent Views (buttons/selects/modals) -> Actionable UI without leaving Discord

## Overview

11 features across 5 new mixins, 6 new SQLite tables, and modifications to ~15
existing files under `integrations/discord/`.

---

## 1. Server Scaffolding (`/setup`)

**What**: A `/setup` command that creates a Discord server structure mirroring
the hub's workspaces, plus the forum tags needed for task triage.

**Target structure**:
```
[Category: frontend-app]
  tasks            (Forum)   -- Bound to workspace. /run creates a post/thread.
  #activity-feed             -- Read-only. Rich embeds for every agent action.
  #approvals                 -- Approval requests with buttons (+ optional role ping).

[Category: backend-api]
  tasks (Forum)
  #activity-feed
  #approvals

[Category: Control Plane]
  #dashboard                 -- Pinned embed: real-time workspace status grid.
  #agent-bus                 -- PMA decisions, cross-workspace handoffs, lifecycle.
  #notifications             -- General lifecycle events.
```

**New config** in `integrations/discord/config.py`:
```python
@dataclass(frozen=True)
class DiscordScaffoldConfig:
    enabled: bool = False
    auto_bind: bool = True
    category_prefix: str = ""

    # Default: Forum, not text+threads.
    tasks_channel_kind: str = "forum"  # "forum" | "text"
    tasks_channel_name: str = "tasks"
    task_forum_tags: tuple[str, ...] = (
        "queued",
        "running",
        "needs-approval",
        "blocked",
        "done",
        "failed",
        "timeout",
        "stopped",
        "p0",
        "p1",
        "p2",
    )

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

**New state table** `discord_forum_tags`:
```sql
CREATE TABLE IF NOT EXISTS discord_forum_tags (
    guild_id INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    forum_channel_id INTEGER NOT NULL,
    tag_name TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, workspace_id, tag_name)
)
```

**Idempotency**:
- Before creating: consult `discord_scaffolded_channels` + verify channel exists via
  `bot.fetch_channel()`.
- If manually deleted: recreate and update state.
- If forum tags drift: ensure required tags exist; recreate missing tags and update
  `discord_forum_tags`.

**Permissions (channel overwrites)**:
- `#activity-feed`, `#dashboard`, `#agent-bus`: `send_messages=False` for `@everyone`,
  `send_messages=True` for the bot.
- `tasks` forum: deny task creation for read-only roles by denying `send_messages`.
  Threads inherit permissions from the parent forum channel.

**Implementation**: New command mixin `ScaffoldCommands` in
`integrations/discord/handlers/commands/scaffold.py`.

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/handlers/commands/scaffold.py` | **Create** — `ScaffoldCommands` mixin (`/setup`) |
| `integrations/discord/config.py` | Modify — add `DiscordScaffoldConfig` under `discord_bot.scaffold` |
| `integrations/discord/state.py` | Modify — add `discord_scaffolded_channels`, `discord_forum_tags` + CRUD |
| `integrations/discord/handlers/commands_runtime.py` | Modify — register `/setup` command |
| `integrations/discord/service.py` | Modify — add `ScaffoldCommands` to mixin list |
| `integrations/discord/rendering.py` | Modify — add `build_setup_summary_embed()` |

---

## 2. Forum-Backed Tasks (Task-Per-Thread)

**What**: Every task is a forum post/thread in the workspace’s `tasks` forum.
The thread is the isolated execution context (`topic_key`).

**Primary entrypoints**:
- `/run` inside a workspace `tasks` forum (canonical)
- Mention-triggered messages in other bound channels may **create a task post**
  in the workspace `tasks` forum and reply with a link (optional but recommended
  to make “start a task from anywhere” work without scattering contexts).

**Thread naming**: `task-{prompt[:80]}` sanitized. Discord limit: 100 chars.
Fallback: `task-{timestamp}`.

**Creation flow** (forum):
1. Resolve workspace from the current context (bindings/scaffold table).
2. Resolve the workspace `tasks` forum channel id from `discord_scaffolded_channels`.
3. Create a forum thread using `ForumChannel.create_thread(...)` with a starter
   **Task Card** embed and initial tag `queued`.
4. Build `topic_key` using `(guild_id, tasks_forum_channel_id, thread.id)`.
5. Execute the turn in the thread context (send placeholder/progress in-thread).

**State transitions (tags)**:
- `queued` -> `running` when execution starts
- `running` -> `done` on success
- `running` -> `failed` on error
- `running` -> `timeout` on timeout
- Any -> `stopped` on `/stop`
- Any -> `needs-approval` when an approval request is emitted (sticky until resolved)

**Thread lifecycle**:
- Prefer tags as the primary state signal.
- Keep the existing thread rename prefixing as a secondary signal (optional).

**New state table** `discord_tasks`:
```sql
CREATE TABLE IF NOT EXISTS discord_tasks (
    guild_id INTEGER NOT NULL,
    workspace_id TEXT NOT NULL,
    forum_channel_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    root_message_id INTEGER NOT NULL,  -- Task Card (starter message)
    created_by_user_id INTEGER,
    initial_prompt TEXT,
    created_at TEXT NOT NULL,
    last_state TEXT,
    last_state_at TEXT,
    last_activity_message_id INTEGER,
    PRIMARY KEY (guild_id, thread_id)
)
```

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/handlers/commands/execution.py` | Modify — `/run` creates forum task threads and routes execution there |
| `integrations/discord/handlers/messages.py` | Modify — (optional) mention-trigger creates forum task thread + replies with link |
| `integrations/discord/helpers.py` | Modify — add `sanitize_thread_name(prompt) -> str` and helpers to resolve `tasks` forum |
| `integrations/discord/constants.py` | Modify — add `THREAD_NAME_MAX_LEN = 100` (if not already present) |
| `integrations/discord/state.py` | Modify — add `discord_tasks` + helpers for task lookup/listing |

---

## 3. Task Card + Persistent Controls

**What**: The starter message in every forum task thread is a “Task Card” embed
that acts as the stable UI surface for that task (updated throughout execution).

**Task Card embed** (starter message requirements also satisfy forum create_thread):
- Workspace name + path (or id)
- Initiator (Discord user)
- Effective approval mode + sandbox policy
- Status (mirrors tags)
- Timestamps: created / last update / finished
- Jump links:
  - latest agent response (or progress message)
  - latest activity feed entry (optional)

**Task Card controls** (persistent `View`, mobile-friendly):
- `Stop` -> triggers existing interrupt
- `Set approvals` -> select/modal to set safe/yolo (and/or full preset set)
- `Rerun` -> re-runs `initial_prompt` in the same workspace context (uses current model/agent/topic settings)
- `Escalate (p0)` -> applies `p0` tag and pings `alert_role_id` (cooldown enforced)

**Persistence**:
- Store `root_message_id` in `discord_tasks`.
- Re-register persistent views in `on_ready()` using `bot.add_view(...)` so buttons
  survive restarts.

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/handlers/tasks.py` | **Create** — `DiscordTasksMixin` (task card render + button callbacks) |
| `integrations/discord/handlers/callbacks.py` | Modify — route `task:*` custom_ids to `DiscordTasksMixin` |
| `integrations/discord/rendering.py` | Modify — add `build_task_card_embed()` |
| `integrations/discord/service.py` | Modify — add mixin + re-register persistent task views on ready |

---

## 4. Role-Based Access Control (RBAC) + Native Command Permissions

**What**: Map Discord roles to permission tiers that control command access and
approval modes. Enforce in code, and use Discord’s app command permission defaults
as defense-in-depth (better UX and fewer accidents).

**New config**:
```yaml
discord_bot:
  rbac:
    enabled: true
    use_default_member_permissions: true
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

**Enforcement**:
- Allowlist remains first-pass gate (guild/channel/role/user dimensions).
- RBAC is second-pass: per-command capability control within allowed contexts.
- Approval mode resolution: topic override > RBAC tier > config default.

**Native command permissions**:
- Register `/setup` with a restrictive `default_member_permissions` (admin-ish).
- Keep RBAC checks authoritative (Discord defaults are not a security boundary).

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/handlers/rbac.py` | **Create** — `DiscordRBACMixin` |
| `integrations/discord/config.py` | Modify — add `DiscordRBACConfig`, `DiscordRoleTier` |
| `integrations/discord/service.py` | Modify — add mixin |
| `integrations/discord/handlers/commands_runtime.py` | Modify — permission checks and set `default_member_permissions` where relevant |

---

## 5. Agent Communication Bus

**What**: A `#agent-bus` channel showing all cross-agent coordination as
structured embeds, with webhook-per-agent for distinct identities.

### 5a. Lifecycle Event Bridge

Register a listener with `LifecycleEventEmitter` (in `core/lifecycle_events.py`)
during `on_ready` and bridge into the bot event loop with
`asyncio.run_coroutine_threadsafe(...)`.

### 5b. PMA Decision Posting

`LifecycleEventType.DISPATCH_CREATED` events already fire when PMA creates
dispatches. The listener posts structured embeds showing source workspace, target,
decision summary, and action items.

### 5c. Cross-Workspace Handoff Signals

When PMA routes a task from workspace A to workspace B (via
`_enqueue_pma_for_lifecycle_event` in `hub.py`), post a handoff embed:
```
[Handoff] frontend-app -> backend-api
"API endpoint needs updating to match new frontend schema"
```

### 5d. Webhook-Per-Agent Identity

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
- **PMA**: "Project Manager" — decisions, orchestration moves
- **Codex**: "Codex Agent" — task updates
- **System**: "Autorunner" — lifecycle events

Each posts via its own webhook with distinct name/avatar.

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/handlers/agent_bus.py` | **Create** — `DiscordAgentBusMixin` |
| `integrations/discord/state.py` | Modify — add `discord_agent_webhooks` |
| `integrations/discord/config.py` | Modify — add `agent_bus_channel_id` |
| `integrations/discord/rendering.py` | Modify — add bus embed builders |
| `integrations/discord/service.py` | Modify — add mixin + register lifecycle listener |
| `integrations/discord/notifications.py` | Modify — optionally route lifecycle events to bus |

---

## 6. Live Dashboard

**What**: A pinned embed in `#dashboard` showing real-time status of all
workspaces. Updated on turn start/complete/error and key lifecycle events.

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

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/handlers/dashboard.py` | **Create** — `DiscordDashboardMixin` |
| `integrations/discord/state.py` | Modify — add `discord_dashboard` |
| `integrations/discord/config.py` | Modify — add `dashboard_channel_id` |
| `integrations/discord/rendering.py` | Modify — add `build_dashboard_embed()` |
| `integrations/discord/service.py` | Modify — add mixin |
| `integrations/discord/handlers/commands/execution.py` | Modify — mark dashboard dirty on turn start/complete |
| `integrations/discord/notifications.py` | Modify — mark dashboard dirty on lifecycle events |

---

## 7. Bot Presence

**What**: Dynamic bot status reflecting swarm activity (e.g., “Watching 2 tasks”).

Called on: turn start, turn complete, and `on_ready`.

**Files**: `integrations/discord/service.py`, `integrations/discord/handlers/commands/execution.py`

---

## 8. Activity Feed

**What**: Per-workspace `#activity-feed` channels with rich embeds for key agent
actions and app-server events.

**Improvements over the original plan**:
- Activity entries should link back to the Task Card (`thread.jump_url` and/or the
  `root_message_id`), so Discord history is navigable.
- When an activity entry is posted, update the Task Card with a link to “latest
  activity”.

**Files**: `integrations/discord/notifications.py`, `integrations/discord/rendering.py`

---

## 9. Alerts (Role Ping)

**What**: Optional role pings for urgent events with dedupe + rate limiting.

**New config** in `integrations/discord/config.py`:
```python
@dataclass(frozen=True)
class DiscordAlertConfig:
    enabled: bool = True
    alert_role_id: Optional[int] = None
    per_task_cooldown_seconds: int = 900
    global_cooldown_seconds: int = 10
```

**Trigger events**:
- `needs-approval` (first request per task, not every update)
- `failed`, `timeout`

**New state table** `discord_alerts`:
```sql
CREATE TABLE IF NOT EXISTS discord_alerts (
    guild_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    alert_type TEXT NOT NULL,   -- 'needs-approval'|'failed'|'timeout'|'p0'
    last_sent_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, thread_id, alert_type)
)
```

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/config.py` | Modify — add `DiscordAlertConfig` under `discord_bot.alerts` |
| `integrations/discord/state.py` | Modify — add `discord_alerts` |
| `integrations/discord/notifications.py` | Modify — send pings on transitions (deduped) |

---

## 10. Triage + Navigation Commands

**What**: Discord-native navigation so the swarm is operable from mobile without
scrolling and searching.

**New commands**:
- `/workspaces`: embed list of workspaces with links/buttons to each workspace’s
  `tasks`, `activity-feed`, and `approvals`.
- `/tasks list [workspace] [tag]`: show tasks filtered by tag/state (backed by SQLite).
- `/tasks mine`: show tasks created by the caller.

**Implementation note**: do not scan Discord history for these; use `discord_tasks`
and (forum) tag state persisted in SQLite for fast, deterministic listings.

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/handlers/commands_runtime.py` | Modify — register new commands |
| `integrations/discord/handlers/commands_spec.py` | Modify — add specs for help/registration |
| `integrations/discord/handlers/tasks.py` | Modify — implement listing + workspace link rendering |
| `integrations/discord/rendering.py` | Modify — add `build_workspaces_embed()`, `build_tasks_list_embed()` |

---

## 11. Reconciliation / Self-Healing

**What**: On `on_ready()`, reconcile Discord state against persisted state and
ensure the control surface stays intact after manual changes or partial failures.

**Reconcile responsibilities**:
- Scaffolded channels exist (recreate if missing).
- Forum tags exist on each `tasks` forum (recreate if missing).
- Dashboard pinned message exists (recreate if missing).
- Persistent views are re-registered (approvals + task cards).

**Observability**:
- Emit structured log events:
  - `discord.reconcile.channel_recreated`
  - `discord.reconcile.tag_recreated`
  - `discord.reconcile.dashboard_recreated`

**Files**:
| File | Action |
|------|--------|
| `integrations/discord/service.py` | Modify — run reconcile pass in `on_ready()` |
| `integrations/discord/state.py` | Modify — helpers to enumerate scaffold + tasks |

---

## New Mixin Summary

| Mixin | File | Added to `integrations/discord/service.py` |
|-------|------|--------------------------------------------|
| `ScaffoldCommands` | `integrations/discord/handlers/commands/scaffold.py` | Yes |
| `DiscordRBACMixin` | `integrations/discord/handlers/rbac.py` | Yes |
| `DiscordAgentBusMixin` | `integrations/discord/handlers/agent_bus.py` | Yes |
| `DiscordDashboardMixin` | `integrations/discord/handlers/dashboard.py` | Yes |
| `DiscordTasksMixin` | `integrations/discord/handlers/tasks.py` | Yes |

Updated mixin list in `integrations/discord/service.py`:
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
    DiscordTasksMixin,         # NEW
):
```

---

## State Schema Migration

Bump `DISCORD_SCHEMA_VERSION` from 1 to 2 in `integrations/discord/state.py`.
Migration creates (if missing) all new tables:
- `discord_scaffolded_channels`
- `discord_forum_tags`
- `discord_tasks`
- `discord_alerts`
- `discord_agent_webhooks`
- `discord_dashboard`

All new tables use `IF NOT EXISTS` for safety. Reconcile logic handles drift.

---

## Implementation Order

| Phase | Feature | Key files |
|-------|---------|-----------|
| 1 | Config + State schema | `integrations/discord/config.py`, `integrations/discord/state.py`, `integrations/discord/constants.py` |
| 2 | Scaffold + forum tags | `integrations/discord/handlers/commands/scaffold.py`, `integrations/discord/state.py` |
| 3 | Forum-backed task creation | `integrations/discord/handlers/commands/execution.py`, `integrations/discord/helpers.py` |
| 4 | Task cards + controls | `integrations/discord/handlers/tasks.py`, `integrations/discord/handlers/callbacks.py`, `integrations/discord/rendering.py` |
| 5 | RBAC + native command perms | `integrations/discord/handlers/rbac.py`, `integrations/discord/handlers/commands_runtime.py` |
| 6 | Dashboard + presence | `integrations/discord/handlers/dashboard.py`, `integrations/discord/service.py` |
| 7 | Agent bus | `integrations/discord/handlers/agent_bus.py`, `integrations/discord/notifications.py` |
| 8 | Activity feed links | `integrations/discord/notifications.py`, `integrations/discord/rendering.py` |
| 9 | Alerts | `integrations/discord/notifications.py`, `integrations/discord/state.py` |
| 10 | Triage commands | `integrations/discord/handlers/commands_runtime.py`, `integrations/discord/handlers/tasks.py` |
| 11 | Reconcile pass | `integrations/discord/service.py`, `integrations/discord/state.py` |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Channel creation rate limit (~10/10min) | Batch creates in `/setup` with small sleeps; be resilient via reconcile |
| Forum starter message requirement | Always create thread with Task Card embed as the starter |
| Webhook limit (15/channel) | Share webhooks; 3 identities well under limit |
| Embed size limits | Keep Task Card compact; move long content to thread messages or attachments |
| Edit rate limits (~5 edits/5s per channel) | Existing progress stream throttling; also throttle Task Card updates |
| Role ping noise | Dedupe per task + global cooldown; disable if `alert_role_id` unset |

---

## Verification

1. **Schema**: Start bot, verify SQLite contains all new tables with correct schema.
2. **`/setup`**: Run in test guild, verify categories + forum/task channels + tag set.
3. **`/setup` idempotency**: Run again, verify no duplicates; delete one tag and rerun to recreate.
4. **Forum `/run`**: Run a task in `tasks` forum; verify a forum post/thread created with Task Card.
5. **Tag transitions**: Verify `queued -> running -> done/failed/timeout` tags apply correctly.
6. **Approvals**: Trigger approval; verify tag `needs-approval` and ping behavior (deduped).
7. **Task controls**: Stop/Set approvals/Rerun/Escalate buttons work; survive restart.
8. **Triage commands**: `/tasks list` filters correctly without scanning history.
9. **Reconcile**: Delete a scaffolded channel or dashboard message, restart bot, verify it is restored and logged.
