# orca-fleet-stats usage

## Commands

```sh
orca-fleet-stats report
orca-fleet-stats report --since 7d --agent claude --tool Bash
orca-fleet-stats report --project my-repo --period day --limit 30
orca-fleet-stats report --host my-server
orca-fleet-stats report --host local --host my-server
orca-fleet-stats report --json
orca-fleet-stats report --html stats.html
orca-fleet-stats collect --since 30d
```

## Flags

| Flag | Meaning |
|---|---|
| `--since` | `YYYY-MM-DD`, or `7d`, `4w`, `3m` |
| `--agent` | `claude`, `codex` or `opencode`; repeatable |
| `--project` | case-insensitive substring of the project name |
| `--tool` | case-insensitive substring of the normalized tool name |
| `--host` | Orca environment name or `local`; repeatable, default all Orca hosts |
| `--period` | `day`, `week` (default) or `month` for the time table |
| `--limit` | rows per table, default 15 |
| `--json` | machine-readable output |
| `--html FILE` | static report with inline CSS and SVG, light and dark |
| `--cache-file NAME` | also return `~/.cache/claude-bar/NAME` (JSON) from each host; repeatable |

## Names

MCP tools become `mcp:<server>/<tool>`. `Task` becomes `Agent`. Codex `exec_command` and
opencode `bash` become `Bash`, and Bash calls are also grouped by the first command word.
Git worktrees under `.../Worktrees/<repo>/<slug>` are reported as `<repo>`.

## Hosts

```sh
orca host list
orca environment list --json
```

Names accepted by `--host` are `local` and the environment names from those commands.

## Cost (API-price estimate)

Tokens and an estimated USD per message, from `message.usage` in Claude Code transcripts
(including subagent files), deduplicated by `message.id:requestId`. Priced with an embedded
per-million-token table (input, output, 5m cache write, 1h cache write, cache read, plus
$0.01 per web search), mirroring claudebar's `costs.py`. Days are local time. Projects are the
git root of the message `cwd`; `Worktrees/<repo>/<slug>` and `.claude/worktrees/<n>` map to
`<repo>`. Codex (`token_count` deltas) and opencode (`tokens` per assistant message) report
tokens; they get USD only when the model is in the table (opencode `claude-*` models are,
Codex GPT models are not and are listed in `unpriced_models`). Filters `--since`, `--project`,
`--agent` and `--host` apply; `--tool` does not.

`--json` adds a top-level `cost` object:

```json
"cost": {
  "total": {"n": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0.0},
  "host": [{"key": "fedora", "n": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0.0}],
  "agent": [], "model": [], "project": [],
  "per_day": [], "per_week": [], "per_month": [],
  "unpriced_models": ["gpt-5"]
}
```

Every list row has the same fields as `total` plus `key`. `n` is messages; `input` is uncached
input tokens; `usd` is rounded to 4 decimals. Lists are sorted by `usd` descending, `per_*` by
key ascending. `collect` output gains `usage`: rows of `day, agent, project, model` with the
same counters, merged across hosts by `report`.

## Cache files

`--cache-file NAME` (on `report` and `collect`) reads `~/.cache/claude-bar/NAME` on each host
in the same run, for example the claude bar pace samples. Names are plain file names (no `/`,
no leading dot, no `..`); files over 1 MiB, missing files and invalid JSON come back as `null`.
`report --json` adds `"files": {"<host label>": {"<NAME>": <parsed JSON or null>}}`.
