<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/logo-dark.svg">
    <img src=".github/logo-light.svg" width="400" height="64" alt="orca-fleet-stats">
  </picture>
</p>

<p align="center">
  Tool-call, latency and API-cost statistics for your coding agents, across every machine your Orca can reach.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <a href="https://github.com/Aethryl/orca-fleet-stats/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Aethryl/orca-fleet-stats/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776ab.svg">
</p>

[Orca](https://github.com/stablyai/orca) is an agent runtime that runs coding agents such as
Claude Code and Codex in git worktrees, on your own machine and on paired remote servers.
orca-fleet-stats is a Claude Code plugin and a standalone CLI that reads the transcripts those
agents leave behind and tells you what they actually did.

## What it does

- Counts tool calls per tool, with MCP tool names normalized and Bash calls also grouped by
  their first command word (`git`, `pytest`, `rg`, ...).
- Reports the error rate and latency (mean, median, p95) of each tool. Latency is the time from
  a tool call to its result in the transcript.
- Counts sessions and subagent spawns by `subagent_type` and model, and splits main-agent from
  subagent activity.
- Estimates cost: tokens and an API-price USD figure per host, agent, model, project and period.
- Breaks everything down by agent, host, project, model and day, week or month, with filters
  for each.
- Merges the numbers from this machine and every paired Orca server into one report, without
  ssh and without installing anything on the servers.

Sources it reads, all read-only. Missing sources are skipped.

| Agent | Read from |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`, including subagent transcripts |
| Codex | `~/.codex/sessions/**/*.jsonl` |
| opencode | `~/.local/share/opencode/opencode.db` |

## Install

As a Claude Code plugin:

```text
/plugin marketplace add Aethryl/orca-fleet-stats
/plugin install orca-fleet-stats@orca-fleet-stats
/reload-plugins
```

That adds the `/orca-fleet-stats` command, the `orca-fleet-stats` skill (so you can just ask
"which tools fail most this week?") and puts `orca-fleet-stats` on PATH.

Without the plugin, run the script directly. It is a single stdlib-only Python file (3.12+):

```sh
python3 -I plugins/orca-fleet-stats/bin/orca_fleet_stats.py report
```

`report` needs the `orca` CLI on PATH, because it asks Orca which hosts exist. `collect` (this
machine only) needs nothing but Python.

## Usage

```sh
orca-fleet-stats report                          # tables, all Orca hosts
orca-fleet-stats report --since 7d --agent claude --tool Bash
orca-fleet-stats report --project my-repo --period day --limit 30
orca-fleet-stats report --host local --host my-server
orca-fleet-stats report --json                   # machine-readable
orca-fleet-stats report --html stats.html        # static page, light and dark
orca-fleet-stats collect --since 30d             # this machine's JSON summary
```

| Flag | Meaning |
|---|---|
| `--since` | `YYYY-MM-DD`, or `7d`, `4w`, `3m` |
| `--agent` | `claude`, `codex` or `opencode`; repeatable |
| `--project` | case-insensitive substring of the project name |
| `--tool` | case-insensitive substring of the normalized tool name |
| `--host` | Orca environment name or `local`; repeatable, default all Orca hosts |
| `--period` | `day`, `week` (default) or `month` for the time tables |
| `--limit` | rows per table, default 15 |
| `--json` | the full result as JSON |
| `--html FILE` | a self-contained HTML report with inline CSS and SVG charts |

Field-level details of the JSON and the cost model are in
[references/usage.md](plugins/orca-fleet-stats/skills/orca-fleet-stats/references/usage.md).

## Output example

Trimmed, from synthetic data:

```text
$ orca-fleet-stats report --limit 4
hosts: local, build-box
calls 509  errors 18 (3.5%)  sessions 20  mean 8.9s  median 128ms  p95 9.7s

By host
name       calls  err%   mean  median    p95  sessions
local        285   3.9  10.4s   128ms  11.6s        12
build-box    224   3.1   7.0s   128ms   8.2s         8

By tool
name  calls  err%  mean  median    p95  sessions
Read    178   1.7  98ms    91ms  181ms         -
Bash    142   8.5  3.6s    1.7s  11.6s         -
Edit     87   1.1  98ms    91ms  181ms         -
Grep     68   1.5  90ms    91ms  181ms         -
... 2 more

Bash by command
command  calls  err%  mean  median    p95
git         35   2.9  4.1s    1.7s  23.2s
ls          29   6.9  3.1s    2.0s   9.7s
rg          28   7.1  2.8s    1.7s   8.2s
pytest      26  11.5  4.6s    2.0s  13.8s
... 1 more

Cost (API-price estimate): $21.83  input 13.9k  output 246.4k  cache write 1.1M  cache read 28.2M  msgs 509

Cost by model
model                 usd  input  output  cache_w  cache_r  msgs
claude-opus-4-7    $13.67   7.1k  123.6k   562.3k    14.1M   254
claude-sonnet-4-6   $8.16   6.8k  122.8k   547.6k    14.2M   255

Subagent spawns
subagent_type  model  agent  spawns
general-purpose  sonnet  claude  10
Explore  sonnet  claude  6
```

The full report also has tables by agent, period, project, model, and main agent vs subagent,
and cost by host, agent, project and period.

## How it works

**Collecting.** On each machine, `collect` parses the transcripts and aggregates them into
buckets keyed by day, agent, project, tool, command word, model and main/subagent, plus a
short per-session list (session id, project, call count, tool names). Only that summary leaves
the machine: no prompts, tool inputs or outputs. Worktrees under `.../Worktrees/<repo>/<branch>`
are reported as `<repo>`.

**Errors.** A call fails when Claude Code marks the result `is_error`, when a Codex command
exits non-zero, or when opencode records a failed tool state.

**Latency.** Each call's latency goes into a log-scale histogram with four buckets per
doubling, so summaries from many machines can be merged exactly. Median and p95 are read from
that histogram and are accurate to about 19%. Parallel calls and tools that wait on the user
inflate the mean, so the median is the better number to read.

**Cost.** Token counts come from each transcript's usage records: Claude Code `message.usage`
(main agent and subagents, de-duplicated per message), Codex `token_count` deltas and opencode
per-message tokens. They are priced with a built-in per-model API price table, including the
5-minute and 1-hour cache-write rates and web searches. Models not in the table are reported
with tokens only. It is an estimate of what the same usage would cost at API prices, not a
bill.

**Remote hosts.** `report` lists hosts with `orca host list`: this machine plus each paired
Orca server. SSH targets registered in Orca are not used. For each server it:

1. opens a terminal in one of the server's existing worktrees (`orca terminal create --environment`);
2. uploads the script itself as gzip+base64 through `orca terminal send` into a `mktemp` directory;
3. runs `collect` there with `python3`;
4. reads the result back with `orca terminal read` as sentinel-delimited, sha256-checked base64 lines;
5. removes the temp directory and closes the terminal, also on failure.

A host that fails is reported on stderr and skipped. The server needs `sh`, `python3` and
`mktemp`, and has to be paired and reachable from your Orca.

## Development

```sh
uv run --no-project --with pytest pytest
uvx ruff check . && uvx ruff format --check .
```

## Licence

[MIT](LICENSE)
