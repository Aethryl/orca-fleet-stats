<h1 align="center">orca-fleet-stats</h1>

<p align="center">
  Activity statistics from AI coding agent transcripts, across every machine your Orca can reach.
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776ab.svg">
</p>

How often are tools called, how often do they fail, how slow are they, how many sessions
and subagents, per project, agent, model, host and day, week or month. Stdlib-only Python,
nothing to install on remote machines. Remote hosts are reached through Orca, no ssh.

## Install

```text
/plugin marketplace add Aethryl/orca-fleet-stats
/plugin install orca-fleet-stats@orca-fleet-stats
/reload-plugins
```

That adds the `/orca-fleet-stats` command, the `orca-fleet-stats` skill and puts `orca-fleet-stats` on PATH.
The CLI also runs standalone: `python3 -I plugins/orca-fleet-stats/bin/orca_fleet_stats.py report`.

## Sources

| Agent | Read from |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`, including subagent transcripts |
| Codex | `~/.codex/sessions/**/*.jsonl` |
| opencode | `~/.local/share/opencode/opencode.db` |

Missing sources are skipped.

## Usage

```sh
orca-fleet-stats report                          # tables
orca-fleet-stats report --since 7d --agent claude --tool Bash
orca-fleet-stats report --project my-repo --period day
orca-fleet-stats report --json
orca-fleet-stats report --html stats.html        # static, light and dark
```

Metrics: calls per tool (MCP names normalized, Bash also grouped by command), error rate,
latency mean, median and p95 (tool call to result), sessions, and subagent spawns by
`subagent_type` and model. Median and p95 come from a log histogram, accurate to about 19%.
Cost: tokens and an API-price USD estimate per host, agent, model, project and period (Claude Code transcripts incl. subagents; Codex/opencode tokens, USD only for priced models). Shape in `references/usage.md`.

## Hosts

By default `report` covers every host from `orca host list`: this machine plus each paired
Orca server (saved environment). Pick some with `--host`:

```sh
orca-fleet-stats report --host thanatos
orca-fleet-stats report --host local --host thanatos
```

Names are Orca environment names, or `local`. Requires the `orca` CLI on PATH and each server
paired and reachable. SSH targets registered in Orca are not used.

For a server, the tool opens a terminal in one of its existing worktrees
(`orca terminal create --environment`), uploads the collector as gzip+base64 through
`terminal send`, runs it with `python3` from a `mktemp` directory, and reads the result back
with `terminal read` as sentinel-delimited, checksummed base64 lines. The terminal is always
closed and the temp directory removed, also on failure. A failing host is reported on stderr
and skipped. The remote needs `sh`, `python3` and `mktemp`.

`collect` prints the per-machine JSON summary (aggregated counts, no transcript content).

## Development

```sh
uv run --no-project --with pytest pytest
uvx ruff check . && uvx ruff format --check .
```

## License

MIT
