---
name: orca-fleet-stats
description: Use when asked how much or how well coding agents were used, by tool, machine, project or agent.
---

# Agent activity statistics

`orca-fleet-stats` reads local transcripts of Claude Code, Codex and opencode and reports
activity: calls per tool, error rate, latency (mean, median, p95), sessions and subagent
spawns, split by agent, project, model, host and day, week or month. It is on PATH while
this plugin is enabled. Sources that do not exist on a machine are skipped.

Run `orca-fleet-stats report` and answer from its output. Start with the default table, then
narrow with filters when the question names a time range, project, agent or tool. Use
`--json` when you need to compute on the numbers, and `--html FILE` when the user wants
something to open or share.

By default the report covers every host Orca knows (`orca host list`): this machine plus each
paired Orca server. Narrow with `--host <environment name>` or `--host local`, repeatable. Remote
hosts are driven through the `orca` CLI (a temporary terminal that is always closed), with no
ssh and nothing to install there. A host that fails is reported on stderr and skipped, so say
which host is missing from the answer. Orca must be running and the server paired.

Latency is the gap between a tool call and its result in the transcript. Parallel calls and
tools that wait on the user (AskUserQuestion) inflate it, so read the median, not the mean.
Median and p95 are approximate, within about 19 percent. Error rate counts `is_error`
results, non-zero exit codes for Codex, and failed tool states for opencode.

Do not edit the transcripts. The tool only reads them. Never open or close Orca terminals
yourself for this; the tool manages its own.

Flags, config format and example invocations are in
[references/usage.md](references/usage.md).
