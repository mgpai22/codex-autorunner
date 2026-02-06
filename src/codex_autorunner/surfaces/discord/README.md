# Discord Surface

The Discord surface provides CLI entry points for the Discord bot integration.

## Responsibilities

- Launch and manage the Discord gateway bot (`car discord start`)
- Run health diagnostics (`car discord health`)
- Verify state database schema (`car discord state-check`)

## Dependencies

- `integrations.discord` - Bot service, config, state store, handlers
- `core` - Config loading, state management, logging

## CLI Commands

| Command | Description |
|---------|-------------|
| `car discord start --path <root>` | Start the Discord gateway bot |
| `car discord health --path <root>` | Run health diagnostics |
| `car discord state-check --path <root>` | Verify SQLite schema |

## Architecture

```
surfaces/discord/ (this package)
    |
    v
surfaces/cli/cli.py (discord_app Typer group)
    |
    v
integrations/discord/service.py (DiscordBotService)
    |
    v
integrations/discord/* (handlers, state, transport, etc.)
```

The surface layer is thin: it loads configuration, instantiates the service,
and delegates to `DiscordBotService.run_gateway()`. All bot logic lives in
the `integrations/discord/` package.

## Related Documentation

- `docs/discord/architecture.md` - Architecture overview
- `docs/discord/security.md` - Security posture
- `docs/discord/discord-integration.md` - Normative specifications
- `docs/ops/discord-bot-runbook.md` - Operational runbook
