---
description: Report coding agent activity statistics (tool calls, errors, latency, sessions, subagents)
argument-hint: "[--since 7d] [--agent claude|codex|opencode] [--project X] [--tool X] [--host <orca-env>|local]..."
allowed-tools: Bash(orca-fleet-stats:*)
---

Run `orca-fleet-stats report $ARGUMENTS` and summarize the result tersely: totals, the biggest
tools and projects, notable error rates or slow tools, and subagent usage, and the API-price cost estimate. With no `--host` it covers all Orca hosts (this machine plus paired servers). If a host failed,
say so. With no arguments, use the default report.
