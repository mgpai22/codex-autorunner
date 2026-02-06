from __future__ import annotations

# ---------------------------------------------------------------------------
# Discord platform limits
# ---------------------------------------------------------------------------
DISCORD_MAX_MESSAGE_LENGTH = 2000
DISCORD_EMBED_DESC_LIMIT = 4096
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_FIELDS_LIMIT = 25
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_SELECT_OPTIONS_LIMIT = 25
DISCORD_CUSTOM_ID_LIMIT = 100
DISCORD_THREAD_AUTO_ARCHIVE_MINUTES = 1440
DISCORD_INTERACTION_TIMEOUT_SECONDS = 3.0

# ---------------------------------------------------------------------------
# Pagination / list defaults
# ---------------------------------------------------------------------------
DEFAULT_PAGE_SIZE = 10
DEFAULT_MODEL_LIST_LIMIT = 25
DEFAULT_MCP_LIST_LIMIT = 50
DEFAULT_SKILLS_LIST_LIMIT = 50
MAX_TOPIC_THREAD_HISTORY = 50

# ---------------------------------------------------------------------------
# Timeouts & TTLs (mirrored from Telegram surface)
# ---------------------------------------------------------------------------
DEFAULT_INTERRUPT_TIMEOUT_SECONDS = 30.0

DEFAULT_AGENT_TURN_TIMEOUT_SECONDS = {
    "codex": 28800.0,
    "opencode": 28800.0,
}

APP_SERVER_START_BACKOFF_INITIAL_SECONDS = 1.0
APP_SERVER_START_BACKOFF_MAX_SECONDS = 30.0
CACHE_CLEANUP_INTERVAL_SECONDS = 300.0
COALESCE_BUFFER_TTL_SECONDS = 60.0
MEDIA_BATCH_BUFFER_TTL_SECONDS = 60.0
MODEL_PENDING_TTL_SECONDS = 1800.0
PENDING_APPROVAL_TTL_SECONDS = 600.0
PENDING_QUESTION_TTL_SECONDS = 600.0
REASONING_BUFFER_TTL_SECONDS = 900.0
SELECTION_STATE_TTL_SECONDS = 1800.0
TURN_PREVIEW_TTL_SECONDS = 900.0
PROGRESS_STREAM_TTL_SECONDS = 900.0
OVERSIZE_WARNING_TTL_SECONDS = 3600.0
UPDATE_ID_PERSIST_INTERVAL_SECONDS = 60.0

# ---------------------------------------------------------------------------
# Outbox retry
# ---------------------------------------------------------------------------
OUTBOX_RETRY_INTERVAL_SECONDS = 10.0
OUTBOX_IMMEDIATE_RETRY_DELAYS = (0.5, 2.0, 5.0)
OUTBOX_MAX_ATTEMPTS = 8

# ---------------------------------------------------------------------------
# Progress / streaming display
# ---------------------------------------------------------------------------
# Discord rate-limits message edits to ~5 per 5 s per channel; use a
# stricter interval than Telegram's 1.0 s to stay safely under the limit.
PROGRESS_STREAM_MIN_EDIT_INTERVAL_SECONDS = 1.5
STREAM_PREVIEW_PREFIX = ""
THINKING_PREVIEW_MAX_LEN = 80
THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS = 1.5
TURN_PROGRESS_MAX_LEN = 160
TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.5
TURN_PROGRESS_TTL_SECONDS = 900.0
PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Embed colours
# ---------------------------------------------------------------------------
EMBED_COLOR_INFO = 0x3498DB
EMBED_COLOR_SUCCESS = 0x2ECC71
EMBED_COLOR_ERROR = 0xE74C3C
EMBED_COLOR_WARNING = 0xF39C12
EMBED_COLOR_PROGRESS = 0x3498DB

# ---------------------------------------------------------------------------
# Status icons
# ---------------------------------------------------------------------------
STATUS_ICONS = {
    "done": "\u2713",
    "fail": "\u2717",
    "warn": "\u26a0",
    "running": "\u25b8",
    "update": "\u21bb",
    "thinking": "\U0001f9e0",
}

# ---------------------------------------------------------------------------
# Agent / model defaults
# ---------------------------------------------------------------------------
DEFAULT_AGENT = "codex"
VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
VALID_AGENT_VALUES = {"codex", "opencode"}
DEFAULT_AGENT_MODELS = {
    "codex": "gpt-5.3-codex",
    "opencode": "zai-coding-plan/glm-4.7",
}
LEGACY_DEFAULT_AGENT_MODELS = DEFAULT_AGENT_MODELS
CONTEXT_BASELINE_TOKENS = 12000

# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------
APPROVAL_POLICY_VALUES = {"untrusted", "on-failure", "on-request", "never"}
APPROVAL_PRESETS = {
    "read-only": ("on-request", "readOnly"),
    "auto": ("on-request", "workspaceWrite"),
    "full-access": ("never", "dangerFullAccess"),
}

# ---------------------------------------------------------------------------
# Command / picker prompts  (adapted: "buttons below" -> "menu below")
# ---------------------------------------------------------------------------
COMMAND_DISABLED_TEMPLATE = "'/{name}' is disabled while a task is in progress."
RESUME_PICKER_PROMPT = "Select a thread to resume (menu below or reply with number/id)."
BIND_PICKER_PROMPT = "Select a repo to bind (menu below or reply with number/id)."
AGENT_PICKER_PROMPT = "Select an agent (menu below)."
MODEL_PICKER_PROMPT = "Select a model (menu below)."
EFFORT_PICKER_PROMPT = "Select a reasoning effort for {model}."
UPDATE_PICKER_PROMPT = "Select update target (menu below)."
REVIEW_COMMIT_PICKER_PROMPT = (
    "Select a commit to review (menu below or reply with number)."
)
FLOW_RUNS_PICKER_PROMPT = "Select a ticket flow run (menu below)."
REVIEW_COMMIT_BUTTON_LABEL_LIMIT = 80

UPDATE_TARGET_OPTIONS = (
    ("both", "Both (web + Discord)"),
    ("web", "Web only"),
    ("discord", "Discord only"),
)

# ---------------------------------------------------------------------------
# Placeholder text
# ---------------------------------------------------------------------------
PLACEHOLDER_TEXT = "Working..."
QUEUED_PLACEHOLDER_TEXT = "Queued (waiting for available worker...)"

# ---------------------------------------------------------------------------
# Trace / error tokens
# ---------------------------------------------------------------------------
TRACE_MESSAGE_TOKENS = (
    "failed",
    "error",
    "denied",
    "unknown",
    "not bound",
    "not found",
    "invalid",
    "unsupported",
    "disabled",
    "missing",
    "mismatch",
    "different workspace",
    "no previous",
    "no resumable",
    "no workspace-tagged",
    "not applicable",
    "selection expired",
    "timed out",
    "timeout",
    "aborted",
    "canceled",
    "cancelled",
)

# ---------------------------------------------------------------------------
# State / server defaults
# ---------------------------------------------------------------------------
DEFAULT_STATE_FILE = ".codex-autorunner/discord_state.sqlite3"
DEFAULT_APP_SERVER_COMMAND = ["codex", "app-server"]
DEFAULT_APP_SERVER_MAX_HANDLES = 20
DEFAULT_APP_SERVER_IDLE_TTL_SECONDS = 3600
DEFAULT_APP_SERVER_START_TIMEOUT_SECONDS = 30
DEFAULT_APP_SERVER_TURN_TIMEOUT_SECONDS = 28800

# ---------------------------------------------------------------------------
# Trigger mode
# ---------------------------------------------------------------------------
DEFAULT_TRIGGER_MODE = "mentions"
TRIGGER_MODE_OPTIONS = {"all", "mentions"}

# ---------------------------------------------------------------------------
# Coalesce / overflow
# ---------------------------------------------------------------------------
DEFAULT_COALESCE_WINDOW_SECONDS = 0.5
MAX_COALESCE_BUFFER_MESSAGES = 20
MAX_COALESCE_DELAY_SECONDS = 10.0
DEFAULT_MESSAGE_OVERFLOW = "split"
MESSAGE_OVERFLOW_OPTIONS = {"split", "trim", "thread"}

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
DEFAULT_METRICS_MODE = "separate"
METRICS_MODE_OPTIONS = {"separate", "append_to_response", "append_to_progress"}

# ---------------------------------------------------------------------------
# Progress stream
# ---------------------------------------------------------------------------
DEFAULT_PROGRESS_STREAM_ENABLED = True
DEFAULT_PROGRESS_STREAM_MAX_ACTIONS = 5
DEFAULT_PROGRESS_STREAM_MAX_OUTPUT_CHARS = 120
# Stricter than Telegram (1.0s) due to Discord rate limits on edits (~5/5s)
DEFAULT_PROGRESS_STREAM_MIN_EDIT_INTERVAL_SECONDS = 1.5
PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 5.0
STREAM_PREVIEW_PREFIX = ""
THINKING_PREVIEW_MAX_LEN = 80
THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS = 1.5
TURN_PROGRESS_MAX_LEN = 160
TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.5
TURN_PROGRESS_TTL_SECONDS = 900.0

# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------
SHELL_OUTPUT_TRUNCATION_SUFFIX = "\n...(truncated)"
SHELL_MESSAGE_BUFFER_CHARS = 200

# ---------------------------------------------------------------------------
# Compact / summary
# ---------------------------------------------------------------------------
COMPACT_MAX_ACTIONS = 10
COMPACT_MAX_TEXT_LENGTH = 80
COMPACT_SUMMARY_PROMPT = (
    "Summarize the conversation so far into a concise context block I can paste into "
    "a new thread. Include goals, constraints, decisions, and current state."
)

# ---------------------------------------------------------------------------
# Init prompt
# ---------------------------------------------------------------------------
INIT_PROMPT = "\n".join(
    [
        "Generate a file named AGENTS.md that serves as a contributor guide for this repository.",
        "Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.",
        "Follow the outline below, but adapt as needed - add sections if relevant, and omit those that do not apply to this project.",
        "",
        "Document Requirements",
        "",
        '- Title the document "Repository Guidelines".',
        "- Use Markdown headings (#, ##, etc.) for structure.",
        "- Keep the document concise. 200-400 words is optimal.",
        "- Keep explanations short, direct, and specific to this repository.",
        "- Provide examples where helpful (commands, directory paths, naming patterns).",
        "- Maintain a professional, instructional tone.",
        "",
        "Recommended Sections",
        "",
        "Project Structure & Module Organization",
        "",
        "- Outline the project structure, including where the source code, tests, and assets are located.",
        "",
        "Build, Test, and Development Commands",
        "",
        "- List key commands for building, testing, and running locally (e.g., npm test, make build).",
        "- Briefly explain what each command does.",
        "",
        "Coding Style & Naming Conventions",
        "",
        "- Specify indentation rules, language-specific style preferences, and naming patterns.",
        "- Include any formatting or linting tools used.",
        "",
        "Testing Guidelines",
        "",
        "- Identify testing frameworks and coverage requirements.",
        "- State test naming conventions and how to run tests.",
        "",
        "Commit & Pull Request Guidelines",
        "",
        "- Summarize commit message conventions found in the project's Git history.",
        "- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).",
        "",
        "(Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions.",
    ]
)

# ---------------------------------------------------------------------------
# Resume / token-usage display limits
# ---------------------------------------------------------------------------
RESUME_BUTTON_PREVIEW_LIMIT = 60
RESUME_PREVIEW_USER_LIMIT = 1000
RESUME_PREVIEW_ASSISTANT_LIMIT = 1000
RESUME_PREVIEW_SCAN_LINES = 200
RESUME_MISSING_IDS_LOG_LIMIT = 10
RESUME_REFRESH_LIMIT = 10
TOKEN_USAGE_CACHE_LIMIT = 256
TOKEN_USAGE_TURN_CACHE_LIMIT = 512

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
THREAD_LIST_PAGE_LIMIT = 100
THREAD_LIST_MAX_PAGES = 5
MAX_MENTION_BYTES = 200_000
DEFAULT_UPDATE_REPO_URL = "https://github.com/Git-on-my-level/codex-autorunner.git"
DEFAULT_UPDATE_REPO_REF = "main"

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
TurnKey = tuple[str, str]
