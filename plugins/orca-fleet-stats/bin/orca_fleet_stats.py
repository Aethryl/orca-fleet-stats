#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

VERSION = 1
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude" / "projects"
CODEX_DIR = HOME / ".codex" / "sessions"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
AGENTS = ("claude", "codex", "opencode")

OPENCODE_TOOLS = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "task": "Agent",
    "webfetch": "WebFetch",
    "todowrite": "TodoWrite",
}
CODEX_TOOLS = {"exec_command": "Bash"}
CLAUDE_TOOLS = {"Task": "Agent"}

ENV_ASSIGN = re.compile(r"^\w+=\S*$")
CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*(?:&&|;)\s*")
WRAPPERS = {"sudo", "time", "env", "command", "nohup", "exec"}
EXIT_CODE = re.compile(r"Process exited with code (-?\d+)")
WORKTREE = re.compile(r"/Worktrees/([^/]+)/")


def norm_tool(agent: str, raw: str) -> str:
    if raw.startswith("mcp__"):
        parts = raw.split("__", 2)
        server = parts[1].removeprefix("plugin_") if len(parts) > 1 else "?"
        return f"mcp:{server}/{parts[2] if len(parts) > 2 else '?'}"
    if agent == "claude":
        return CLAUDE_TOOLS.get(raw, raw)
    if agent == "codex":
        return CODEX_TOOLS.get(raw, raw)
    return OPENCODE_TOOLS.get(raw, raw)


def bash_word(cmd: str) -> str:
    cmd = CD_PREFIX.sub("", cmd or "")
    for tok in cmd.split():
        if ENV_ASSIGN.match(tok) or tok in WRAPPERS:
            continue
        return os.path.basename(tok.strip("\"'("))[:24]
    return ""


def project_name(cwd: str | None, fallback: str = "?") -> str:
    if not cwd:
        return fallback
    m = WORKTREE.search(cwd + "/")
    if m:
        return m.group(1)
    if cwd == str(HOME):
        return "~"
    return os.path.basename(cwd.rstrip("/")) or cwd


def day_of(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_since(s: str | None) -> str | None:
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([dwm])", s)
    if m:
        days = int(m.group(1)) * {"d": 1, "w": 7, "m": 30}[m.group(2)]
        return (date.today() - timedelta(days=days)).isoformat()
    date.fromisoformat(s)
    return s


def new_call(agent: str, sid: str, project: str, ts: float | None, raw: str) -> dict:
    return {
        "agent": agent,
        "sid": sid,
        "project": project,
        "ts": ts,
        "raw": raw,
        "tool": norm_tool(agent, raw),
        "sub": "",
        "err": False,
        "lat": None,
        "model": "",
        "side": False,
        "spawn": None,
    }


def claude_calls(since: str | None = None, root: Path = CLAUDE_DIR) -> Iterator[dict]:
    if not root.is_dir():
        return
    cutoff = datetime.fromisoformat(since).timestamp() if since else 0
    seen: set[str] = set()
    for f in root.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            yield from _claude_file(f, root, seen)
        except OSError:
            continue


def _claude_file(f: Path, root: Path, seen: set[str]) -> Iterator[dict]:
    rel = f.relative_to(root).parts
    fallback = rel[0]
    pend: dict[str, dict] = {}
    cwd = None
    out: list[dict] = []
    with f.open(errors="replace") as fh:
        for line in fh:
            if '"tool_use' not in line and '"tool_result"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                continue
            cwd = d.get("cwd") or cwd
            ts = parse_iso(d.get("timestamp"))
            for b in msg["content"]:
                if not isinstance(b, dict):
                    continue
                kind = b.get("type")
                if kind == "tool_use":
                    bid = b.get("id")
                    if not bid or bid in seen:
                        continue
                    seen.add(bid)
                    c = new_call(
                        "claude",
                        d.get("sessionId") or f.stem,
                        project_name(cwd, fallback),
                        ts,
                        b.get("name", "?"),
                    )
                    inp = b.get("input") or {}
                    c["model"] = msg.get("model") or ""
                    c["side"] = bool(d.get("isSidechain"))
                    if c["tool"] == "Bash":
                        c["sub"] = bash_word(inp.get("command", ""))
                    if c["tool"] == "Agent":
                        c["spawn"] = (inp.get("subagent_type") or "general-purpose", inp.get("model") or "")
                    pend[bid] = c
                    out.append(c)
                elif kind == "tool_result":
                    c = pend.get(b.get("tool_use_id"))
                    if c is None:
                        continue
                    c["err"] = bool(b.get("is_error"))
                    if ts is not None and c["ts"] is not None:
                        c["lat"] = max(0.0, ts - c["ts"])
    yield from out


def codex_calls(since: str | None = None, root: Path = CODEX_DIR) -> Iterator[dict]:
    if not root.is_dir():
        return
    cutoff = datetime.fromisoformat(since).timestamp() if since else 0
    for f in root.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            yield from _codex_file(f)
        except OSError:
            continue


def _codex_file(f: Path) -> Iterator[dict]:
    sid = f.stem
    cwd = None
    model = ""
    side = False
    pend: dict[str, dict] = {}
    out: list[dict] = []
    with f.open(errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload")
            if not isinstance(p, dict):
                continue
            t = d.get("type")
            if t == "session_meta" and cwd is None:
                sid = p.get("session_id") or p.get("id") or sid
                cwd = p.get("cwd")
                side = isinstance(p.get("source"), dict)
            elif t == "turn_context":
                model = p.get("model") or model
                cwd = cwd or p.get("cwd")
            elif t == "response_item":
                pt = p.get("type")
                ts = parse_iso(d.get("timestamp"))
                if pt in ("function_call", "custom_tool_call"):
                    c = new_call("codex", sid, project_name(cwd), ts, p.get("name", "?"))
                    c["model"] = model
                    c["side"] = side
                    if c["tool"] == "Bash":
                        try:
                            args = json.loads(p.get("arguments") or "{}")
                        except ValueError:
                            args = {}
                        c["sub"] = bash_word(args.get("cmd") or args.get("command") or "")
                    pend[p.get("call_id", "")] = c
                    out.append(c)
                elif pt in ("function_call_output", "custom_tool_call_output"):
                    c = pend.get(p.get("call_id", ""))
                    if c is None:
                        continue
                    if ts is not None and c["ts"] is not None:
                        c["lat"] = max(0.0, ts - c["ts"])
                    c["err"] = _codex_failed(p.get("output"))
    yield from out


def _codex_failed(output: object) -> bool:
    if not isinstance(output, str):
        return False
    m = EXIT_CODE.search(output)
    if m:
        return m.group(1) != "0"
    try:
        meta = json.loads(output).get("metadata", {})
    except (ValueError, AttributeError):
        return False
    return meta.get("exit_code", 0) != 0


OPENCODE_SQL = """
select p.session_id, p.data, p.time_created, s.directory, s.parent_id,
       coalesce(json_extract(m.data,'$.modelID'), json_extract(m.data,'$.model.modelID'))
from part p
join session s on s.id = p.session_id
left join message m on m.id = p.message_id
where json_extract(p.data,'$.type') = 'tool'
"""


def opencode_calls(since: str | None = None, db: Path = OPENCODE_DB) -> Iterator[dict]:
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(OPENCODE_SQL).fetchall()
        con.close()
    except sqlite3.Error:
        return
    for sid, data, created, directory, parent, model in rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        state = d.get("state") or {}
        times = state.get("time") or {}
        start = times.get("start") or created
        c = new_call("opencode", sid, project_name(directory), start / 1000 if start else None, d.get("tool", "?"))
        c["model"] = model or ""
        c["side"] = parent is not None
        c["err"] = state.get("status") == "error"
        if times.get("start") and times.get("end"):
            c["lat"] = max(0.0, (times["end"] - times["start"]) / 1000)
        inp = state.get("input") or {}
        if c["tool"] == "Bash":
            c["sub"] = bash_word(inp.get("command", ""))
        if c["tool"] == "Agent":
            c["spawn"] = (inp.get("subagent_type") or "general-purpose", "")
        yield c


PRICES_PER_MTOK = {
    "claude-fable-5": (10, 50, 12.5, 20, 1),
    "claude-fable-5-1": (10, 50, 12.5, 20, 0.25),
    "claude-haiku-4-5": (1, 5, 1.25, 2, 0.1),
    "claude-haiku-4-5-20251001": (1, 5, 1.25, 2, 0.1),
    "claude-haiku-5-5": (0.1, 0.5, 0.125, 0.2, 0.01),
    "claude-mythos-5": (10, 50, 12.5, 20, 1),
    "claude-mythos-5-1": (10, 50, 12.5, 20, 0.25),
    "claude-mythos-preview": (10, 50, 12.5, 20, 1),
    "claude-opus-4-5": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-5-20251101": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-6": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-6-20260205": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-7": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-7-20260416": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-8": (5, 25, 6.25, 10, 0.5),
    "claude-opus-5": (5, 25, 6.25, 10, 0.5),
    "claude-opus-5-5": (4, 20, 5, 8, 0.2),
    "claude-sonnet-4-5": (3, 15, 3.75, 6, 0.3),
    "claude-sonnet-4-5-20250929": (3, 15, 3.75, 6, 0.3),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, 0.3),
    "claude-sonnet-5": (2, 10, 2.5, 4, 0.2),
    "claude-sonnet-5-5": (2, 10, 2.5, 4, 0.1),
}
WEB_SEARCH_COST = 0.01
USAGE_FIELDS = ("input", "output", "cache_write", "cache_read")


def price_for(model: str) -> tuple | None:
    table = PRICES_PER_MTOK
    if model in table:
        return table[model]
    stem = model.rsplit("-", 1)[0] if model[-8:].isdigit() else model
    if stem in table:
        return table[stem]
    candidates = [key for key in table if key.startswith(stem)]
    return table[min(candidates, key=len)] if candidates else None


def usage_cost(usage: dict, price: tuple) -> float:
    p_in, p_out, p_write, p_long, p_read = (x / 1e6 for x in price)
    creation = usage.get("cache_creation") or {}
    long_cache = creation.get("ephemeral_1h_input_tokens") or 0
    short_cache = (usage.get("cache_creation_input_tokens") or 0) - long_cache
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    return (
        (usage.get("input_tokens") or 0) * p_in
        + (usage.get("output_tokens") or 0) * p_out
        + short_cache * p_write
        + long_cache * p_long
        + (usage.get("cache_read_input_tokens") or 0) * p_read
        + searches * WEB_SEARCH_COST
    )


def usage_project(cwd: str) -> str:
    parts = cwd.rstrip("/").split("/")
    if "Worktrees" in parts[:-2]:
        return parts[parts.index("Worktrees") + 1]
    for i in range(1, len(parts) - 2):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return parts[i - 1]
    for depth in range(len(parts), 1, -1):
        if os.path.exists("/".join([*parts[:depth], ".git"])):
            return parts[depth - 1]
    return parts[-1] or "~"


def new_usage(agent: str, project: str, ts: float, model: str, side: bool = False) -> dict:
    return {
        "agent": agent,
        "project": project,
        "ts": ts,
        "model": model,
        "side": side,
        "input": 0,
        "output": 0,
        "cache_write": 0,
        "cache_read": 0,
        "usd": 0.0,
    }


def claude_usage(since: str | None = None, root: Path = CLAUDE_DIR) -> Iterator[dict]:
    if not root.is_dir():
        return
    cutoff = datetime.fromisoformat(since).timestamp() if since else 0
    seen: set[str] = set()
    for f in root.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            with f.open(errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    msg = d.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage, model = msg.get("usage"), msg.get("model")
                    ts = parse_iso(d.get("timestamp"))
                    if not isinstance(usage, dict) or not usage or not model or ts is None or model.startswith("<"):
                        continue
                    ident = f"{msg.get('id')}:{d.get('requestId')}"
                    if ident in seen:
                        continue
                    seen.add(ident)
                    r = new_usage("claude", usage_project(d.get("cwd") or ""), ts, model, bool(d.get("isSidechain")))
                    r["input"] = usage.get("input_tokens") or 0
                    r["output"] = usage.get("output_tokens") or 0
                    r["cache_write"] = usage.get("cache_creation_input_tokens") or 0
                    r["cache_read"] = usage.get("cache_read_input_tokens") or 0
                    price = price_for(model)
                    r["usd"] = usage_cost(usage, price) if price else 0.0
                    yield r
        except OSError:
            continue


CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")


def codex_usage(since: str | None = None, root: Path = CODEX_DIR) -> Iterator[dict]:
    if not root.is_dir():
        return
    cutoff = datetime.fromisoformat(since).timestamp() if since else 0
    for f in root.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            yield from _codex_usage_file(f)
        except OSError:
            continue


def _codex_usage_file(f: Path) -> Iterator[dict]:
    cwd = None
    model = ""
    side = False
    prev = dict.fromkeys(CODEX_FIELDS, 0)
    with f.open(errors="replace") as fh:
        for line in fh:
            if '"token_count"' not in line and '"session_meta"' not in line and '"turn_context"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload")
            if not isinstance(p, dict):
                continue
            t = d.get("type")
            if t == "session_meta" and cwd is None:
                cwd = p.get("cwd")
                side = isinstance(p.get("source"), dict)
            elif t == "turn_context":
                model = p.get("model") or model
                cwd = cwd or p.get("cwd")
            elif t == "event_msg" and p.get("type") == "token_count":
                total = (p.get("info") or {}).get("total_token_usage")
                ts = parse_iso(d.get("timestamp"))
                if not isinstance(total, dict) or ts is None:
                    continue
                cur = {k: total.get(k) or 0 for k in CODEX_FIELDS}
                reset = any(cur[k] < prev[k] for k in CODEX_FIELDS)
                delta = {k: cur[k] if reset else cur[k] - prev[k] for k in CODEX_FIELDS}
                prev = cur
                if not any(delta.values()):
                    continue
                r = new_usage("codex", project_name(cwd), ts, model, side)
                r["input"] = max(0, delta["input_tokens"] - delta["cached_input_tokens"])
                r["output"] = delta["output_tokens"]
                r["cache_write"] = delta["cache_write_input_tokens"]
                r["cache_read"] = delta["cached_input_tokens"]
                yield r


OPENCODE_USAGE_SQL = """
select m.id, m.data, s.directory, s.parent_id
from message m
join session s on s.id = m.session_id
where json_extract(m.data,'$.role') = 'assistant'
"""


def opencode_model(model: str) -> str:
    model = model.rsplit("/", 1)[-1]
    return re.sub(r"(\d)\.(\d)", r"\1-\2", model) if model.startswith("claude-") else model


def opencode_usage(since: str | None = None, db: Path = OPENCODE_DB) -> Iterator[dict]:
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(OPENCODE_USAGE_SQL).fetchall()
        con.close()
    except sqlite3.Error:
        return
    for _mid, data, directory, parent in rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        tokens = d.get("tokens")
        created = (d.get("time") or {}).get("created")
        if not isinstance(tokens, dict) or not created:
            continue
        cache = tokens.get("cache") or {}
        model = opencode_model(d.get("modelID") or "")
        r = new_usage("opencode", project_name(directory), created / 1000, model, parent is not None)
        r["input"] = tokens.get("input") or 0
        r["output"] = tokens.get("output") or 0
        r["cache_write"] = cache.get("write") or 0
        r["cache_read"] = cache.get("read") or 0
        if not any(r[k] for k in USAGE_FIELDS):
            continue
        price = price_for(model)
        if price:
            r["usd"] = usage_cost(
                {
                    "input_tokens": r["input"],
                    "output_tokens": r["output"],
                    "cache_creation_input_tokens": r["cache_write"],
                    "cache_read_input_tokens": r["cache_read"],
                },
                price,
            )
        yield r


def lat_bucket(ms: float) -> int:
    return int(round(math.log2(max(ms, 1.0)) * 4))


def bucket_ms(b: int) -> float:
    return 2 ** (b / 4)


def collect(since: str | None = None, agents: Iterable[str] = AGENTS) -> dict:
    sources = {"claude": claude_calls, "codex": codex_calls, "opencode": opencode_calls}
    usage = {"claude": claude_usage, "codex": codex_usage, "opencode": opencode_usage}
    return collect_from({a: sources[a] for a in agents}, since, {a: usage[a] for a in agents})


def collect_usage(sources: dict, since: str | None) -> list[dict]:
    agg: dict[tuple, dict[str, Any]] = {}
    for source in sources.values():
        for r in source(since):
            day = day_of(r["ts"])
            if since and day < since:
                continue
            key = (day, r["agent"], r["project"], r["model"])
            b = agg.get(key)
            if b is None:
                b = agg[key] = {"n": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0.0}
            b["n"] += 1
            for k in USAGE_FIELDS:
                b[k] += r[k]
            b["usd"] += r["usd"]
    return [
        {"day": k[0], "agent": k[1], "project": k[2], "model": k[3], **v, "usd": round(v["usd"], 6)}
        for k, v in agg.items()
    ]


def collect_from(sources: dict, since: str | None, usage_sources: dict | None = None) -> dict:
    buckets: dict[tuple, dict[str, Any]] = {}
    spawns: dict[tuple, int] = defaultdict(int)
    sessions: dict[tuple, dict[str, Any]] = {}
    for source in sources.values():
        for c in source(since):
            day = day_of(c["ts"])
            if since and (day < since or day == "unknown"):
                continue
            key = (day, c["agent"], c["project"], c["tool"], c["sub"], c["model"], c["side"])
            b: dict[str, Any] = buckets.get(key) or {}
            if not b:
                b = buckets[key] = {"n": 0, "err": 0, "lat_n": 0, "lat_sum": 0, "hist": {}}
            b["n"] += 1
            b["err"] += c["err"]
            if c["lat"] is not None:
                ms = c["lat"] * 1000
                b["lat_n"] += 1
                b["lat_sum"] += ms
                lb = str(lat_bucket(ms))
                b["hist"][lb] = b["hist"].get(lb, 0) + 1
            if c["spawn"]:
                spawns[(day, c["agent"], c["project"], *c["spawn"])] += 1
            sk = (c["agent"], c["sid"])
            s: dict[str, Any] = sessions.get(sk) or {}
            if not s:
                s = sessions[sk] = {
                    "agent": c["agent"],
                    "sid": c["sid"],
                    "project": c["project"],
                    "day": day,
                    "n": 0,
                    "tools": set(),
                }
            s["n"] += 1
            s["tools"].add(c["tool"])
            s["day"] = min(s["day"], day)
    return {
        "version": VERSION,
        "host": socket.gethostname(),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "since": since,
        "buckets": [
            {
                "day": k[0],
                "agent": k[1],
                "project": k[2],
                "tool": k[3],
                "sub": k[4],
                "model": k[5],
                "side": k[6],
                **v,
                "lat_sum": round(v["lat_sum"], 1),
            }
            for k, v in buckets.items()
        ],
        "spawns": [
            {"day": k[0], "agent": k[1], "project": k[2], "subagent_type": k[3], "model": k[4], "n": n}
            for k, n in spawns.items()
        ],
        "sessions": [{**s, "tools": sorted(s["tools"])} for s in sessions.values()],
        "usage": collect_usage(usage_sources or {}, since),
    }


SENTINEL = "ORCA_FLEET"
CHUNK = 900
BATCH = 16
POLL_SECONDS = 0.5
REMOTE_DECODE = "import base64,gzip,sys;sys.stdout.buffer.write(gzip.decompress(base64.b64decode(sys.stdin.read())))"


class HostError(RuntimeError):
    pass


def encode_payload(data: dict, nonce: str, width: int = 100) -> str:
    """Render a summary as sentinel-delimited, gzip+base64 text lines with a sha256 trailer."""
    raw = gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0)
    b64 = base64.b64encode(raw).decode()
    lines = [b64[i : i + width] for i in range(0, len(b64), width)]
    digest = hashlib.sha256("".join(lines).encode()).hexdigest()
    out = ["", f"{SENTINEL}_BEGIN {nonce}", *lines, f"{SENTINEL}_END {nonce} {len(lines)} {digest}"]
    return "\n".join(out) + "\n"


def find_payload(lines: Iterable[str], nonce: str) -> dict | None:
    """Return the decoded summary once a complete, verified block for nonce is present, else None."""
    begin = f"{SENTINEL}_BEGIN {nonce}"
    end = f"{SENTINEL}_END {nonce} "
    fail = f"{SENTINEL}_FAIL {nonce}"
    body: list[str] | None = None
    for raw in lines:
        line = raw.strip()
        if line == fail:
            raise HostError("collector failed on the remote host")
        if line == begin:
            body = []
        elif body is not None and line.startswith(end):
            parts = line[len(end) :].split()
            if len(parts) != 2 or int(parts[0]) != len(body):
                raise HostError("remote output is incomplete or garbled")
            if hashlib.sha256("".join(body).encode()).hexdigest() != parts[1]:
                raise HostError("remote output failed its checksum")
            return json.loads(gzip.decompress(base64.b64decode("".join(body))))
        elif body is not None:
            body.append(line)
    return None


def has_line(lines: Iterable[str], wanted: str) -> bool:
    return any(line.strip() == wanted for line in lines)


def orca(args: list[str], timeout: float = 60) -> dict:
    try:
        r = subprocess.run(["orca", *args, "--json"], capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise HostError("the orca CLI is not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise HostError(f"orca {' '.join(args[:2])} timed out") from e
    try:
        reply = json.loads(r.stdout)
    except ValueError as e:
        raise HostError(r.stderr.decode(errors="replace").strip() or f"orca exit {r.returncode}") from e
    if not reply.get("ok"):
        raise HostError(reply.get("error", {}).get("message", "orca request failed"))
    return reply["result"]


def parse_hosts(result: dict) -> list[dict]:
    """Keep this machine and paired servers from `orca host list`; SSH targets are not used."""
    out = []
    for h in result.get("hosts", []):
        if h.get("kind") == "local":
            out.append({"name": "local", "kind": "local", "label": h.get("machineName") or socket.gethostname()})
        elif h.get("kind") == "environment":
            out.append({"name": h["name"], "kind": "environment", "label": h["name"]})
    return out


def discover_hosts() -> list[dict]:
    return parse_hosts(orca(["host", "list"]))


def select_hosts(hosts: list[dict], wanted: list[str]) -> list[dict]:
    if not wanted:
        return hosts
    by_name = {h["name"]: h for h in hosts}
    unknown = [w for w in wanted if w not in by_name]
    if unknown:
        raise HostError(f"unknown host {', '.join(unknown)}; Orca hosts: {', '.join(by_name)}")
    return [by_name[w] for w in dict.fromkeys(wanted)]


def pick_worktree(result: dict) -> str:
    """Choose an existing worktree id on the server to anchor the terminal's cwd."""
    rows = [w for w in result.get("worktrees", []) if not w.get("isArchived")]
    if not rows:
        raise HostError("the server has no worktree to open a terminal in")
    rows.sort(key=lambda w: not w.get("isMainWorktree"))
    return rows[0]["id"]


class Terminal:
    """A throwaway shell on a paired Orca server, driven only through the orca CLI."""

    def __init__(self, env: str) -> None:
        self.env = env
        self.handle: str | None = None
        self.cursor = "0"
        self.lines: list[str] = []

    def _call(self, group: str, verb: str, *args: str, timeout: float = 60) -> dict:
        return orca([group, verb, "--environment", self.env, *args], timeout)

    def open(self) -> None:
        worktree = pick_worktree(self._call("worktree", "list"))
        res = self._call(
            "terminal", "create", "--worktree", f"id:{worktree}", "--command", "exec sh", "--title", "orca-fleet-stats"
        )
        self.handle = res["terminal"]["handle"]

    def send(self, text: str) -> None:
        assert self.handle
        self._call("terminal", "send", "--terminal", self.handle, "--text", text, "--enter")

    def poll(self) -> list[str]:
        assert self.handle
        res = self._call("terminal", "read", "--terminal", self.handle, "--cursor", self.cursor, "--limit", "5000")
        t = res["terminal"]
        if t.get("truncated") and self.cursor != "0":
            raise HostError("terminal output was truncated before it was read")
        if t.get("status") == "exited":
            raise HostError("remote shell exited")
        self.lines += [ln.rstrip("\r") for ln in t.get("tail", [])]
        if t.get("nextCursor") is not None:
            self.cursor = str(t["nextCursor"])
        return self.lines

    def wait_for(self, done, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            result = done(self.poll())
            if result:
                return result
            if time.monotonic() > deadline:
                raise HostError("timed out waiting for the remote host")
            time.sleep(POLL_SECONDS)

    def close(self) -> None:
        if self.handle:
            try:
                self._call("terminal", "close", "--terminal", self.handle)
            except HostError as e:
                print(f"orca-fleet-stats: could not close terminal {self.handle}: {e}", file=sys.stderr)
            self.handle = None


def upload_lines(script: bytes) -> list[str]:
    b64 = base64.b64encode(gzip.compress(script, mtime=0)).decode()
    return [f"printf %s '{b64[i : i + CHUNK]}' >> \"$T/s.b64\"" for i in range(0, len(b64), CHUNK)]


def fetch_remote(env: str, since: str | None, agents: list[str], timeout: float = 900) -> dict:
    nonce = uuid.uuid4().hex[:12]
    args = f"--encode {nonce}" + (f" --since {since}" if since else "") + "".join(f" --agent {a}" for a in agents)
    term = Terminal(env)
    try:
        term.open()
        probe = f'stty -echo; PS1=; echo "{SENTINEL}_""READY {nonce}"'
        deadline = time.monotonic() + 60
        while True:
            term.send(probe)
            time.sleep(1)
            if has_line(term.poll(), f"{SENTINEL}_READY {nonce}"):
                break
            if time.monotonic() > deadline:
                raise HostError("remote shell did not become ready")
        term.send('T=$(mktemp -d) && : > "$T/s.b64"')
        lines = upload_lines(Path(__file__).read_bytes())
        for i in range(0, len(lines), BATCH):
            term.send("\n".join(lines[i : i + BATCH]))
        term.send(
            f'python3 -c \'{REMOTE_DECODE}\' < "$T/s.b64" > "$T/c.py" && python3 -I "$T/c.py" collect {args}'
            f' || printf \'%s\\n\' "{SENTINEL}_""FAIL {nonce}"'
        )
        return term.wait_for(lambda ls: find_payload(ls, nonce), timeout)
    finally:
        if term.handle:
            try:
                term.send('rm -rf "$T"')
                time.sleep(0.5)
            except HostError as e:
                print(f"orca-fleet-stats: remote cleanup may have failed on {env}: {e}", file=sys.stderr)
        term.close()


def fetch_host(host: dict, since: str | None, agents: list[str]) -> dict:
    data = collect(since, agents) if host["kind"] == "local" else fetch_remote(host["name"], since, agents)
    data["label"] = host["label"]
    return data


class Filters:
    def __init__(self, since=None, agents=None, project=None, tool=None):
        self.since = since
        self.agents = set(agents or [])
        self.project = project
        self.tool = tool

    def ok(self, row: dict) -> bool:
        if self.since and (row["day"] < self.since or row["day"] == "unknown"):
            return False
        if self.agents and row["agent"] not in self.agents:
            return False
        return not self.project or self.project.lower() in row["project"].lower()

    def tool_ok(self, tool: str) -> bool:
        return not self.tool or self.tool.lower() in tool.lower()


def new_stat() -> dict:
    return {"n": 0, "err": 0, "lat_n": 0, "lat_sum": 0.0, "hist": {}}


def add_stat(a: dict, b: dict) -> None:
    a["n"] += b["n"]
    a["err"] += b["err"]
    a["lat_n"] += b["lat_n"]
    a["lat_sum"] += b["lat_sum"]
    for k, v in b["hist"].items():
        a["hist"][k] = a["hist"].get(k, 0) + v


def quantile(hist: dict, q: float) -> float | None:
    total = sum(hist.values())
    if not total:
        return None
    target = q * total
    acc = 0
    for b in sorted(hist, key=int):
        acc += hist[b]
        if acc >= target:
            return bucket_ms(int(b))
    return None


def finish(stat: dict) -> dict:
    n = stat["lat_n"]
    return {
        "n": stat["n"],
        "err": stat["err"],
        "err_pct": round(100 * stat["err"] / stat["n"], 1) if stat["n"] else 0.0,
        "mean_ms": round(stat["lat_sum"] / n) if n else None,
        "median_ms": round(quantile(stat["hist"], 0.5) or 0) if n else None,
        "p95_ms": round(quantile(stat["hist"], 0.95) or 0) if n else None,
    }


def period_keys(day: str) -> dict[str, str]:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return {"day": day, "week": day, "month": day}
    iso = d.isocalendar()
    return {"day": day, "week": f"{iso[0]}-W{iso[1]:02d}", "month": day[:7]}


def new_cost() -> dict:
    return {"n": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "usd": 0.0}


def add_cost(a: dict, b: dict) -> None:
    for k in ("n", *USAGE_FIELDS, "usd"):
        a[k] += b[k]


def finish_cost(c: dict) -> dict:
    return {**c, "usd": round(c["usd"], 4)}


def analyze_cost(hosts: list[dict], f: Filters) -> dict:
    groups: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(new_cost))
    total = new_cost()
    unpriced: set[str] = set()
    for h in hosts:
        for b in h.get("usage", []):
            if not f.ok(b):
                continue
            keys = {
                "host": h["label"],
                "agent": b["agent"],
                "model": b["model"] or "?",
                "project": b["project"],
                **{f"per_{k}": v for k, v in period_keys(b["day"]).items()},
            }
            for g, k in keys.items():
                add_cost(groups[g][k], b)
            add_cost(total, b)
            if price_for(b["model"]) is None:
                unpriced.add(b["model"] or "?")
    out: dict = {"total": finish_cost(total)}
    for g in ("host", "agent", "model", "project", "per_day", "per_week", "per_month"):
        rows = groups.get(g, {})
        out[g] = sorted(
            ({"key": k, **finish_cost(v)} for k, v in rows.items()),
            key=(lambda r: r["key"]) if g.startswith("per_") else (lambda r: -r["usd"]),
        )
    out["unpriced_models"] = sorted(unpriced)
    return out


def analyze(hosts: list[dict], f: Filters) -> dict:
    groups: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(new_stat))
    total = new_stat()
    for h in hosts:
        label = h["label"]
        for b in h["buckets"]:
            if not f.ok(b) or not f.tool_ok(b["tool"]):
                continue
            keys = {
                "tool": b["tool"],
                "agent": b["agent"],
                "project": b["project"],
                "host": label,
                "model": b["model"] or "?",
                "side": "subagent" if b["side"] else "main",
                **{f"per_{k}": v for k, v in period_keys(b["day"]).items()},
            }
            if b["tool"] == "Bash" and b["sub"]:
                keys["bash"] = b["sub"]
            for g, k in keys.items():
                add_stat(groups[g][k], b)
            add_stat(total, b)
    sess: dict[tuple, dict] = {}
    sess_by: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for h in hosts:
        for s in h["sessions"]:
            if not f.ok(s):
                continue
            if f.tool and not any(f.tool_ok(t) for t in s["tools"]):
                continue
            sk = (h["label"], s["agent"], s["sid"])
            sess[sk] = s
            sess_by["agent"][s["agent"]].add(sk)
            sess_by["host"][h["label"]].add(sk)
            sess_by["project"][s["project"]].add(sk)
            for k, v in period_keys(s["day"]).items():
                sess_by[f"per_{k}"][v].add(sk)
    spawns: dict[tuple, int] = defaultdict(int)
    for h in hosts:
        for s in h["spawns"]:
            if f.ok(s):
                spawns[(s["subagent_type"], s["model"] or "-", s["agent"])] += s["n"]
    out: dict = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "hosts": [h["label"] for h in hosts],
        "total": finish(total),
        "sessions": len(sess),
        "cost": analyze_cost(hosts, f),
        "spawns": sorted(
            ({"subagent_type": k[0], "model": k[1], "agent": k[2], "n": n} for k, n in spawns.items()),
            key=lambda r: -r["n"],
        ),
    }
    for g, rows in groups.items():
        out[g] = sorted(
            (
                {"key": k, "sessions": len(sess_by[g][k]) if g in sess_by else None, **finish(v)}
                for k, v in rows.items()
            ),
            key=(lambda r: r["key"]) if g.startswith("per_") else (lambda r: -r["n"]),
        )
    return out


def fmt_ms(v: float | None) -> str:
    if v is None:
        return "-"
    if v >= 60000:
        return f"{v / 60000:.1f}m"
    if v >= 1000:
        return f"{v / 1000:.1f}s"
    return f"{v:.0f}ms"


COLS = [
    ("key", "name"),
    ("n", "calls"),
    ("err_pct", "err%"),
    ("mean_ms", "mean"),
    ("median_ms", "median"),
    ("p95_ms", "p95"),
    ("sessions", "sessions"),
]


def render_table(title: str, rows: list[dict], limit: int, label: str = "name", with_sessions: bool = True) -> str:
    cols = [c for c in COLS if with_sessions or c[0] != "sessions"]
    cells = []
    for r in rows[:limit]:
        row = []
        for k, _ in cols:
            v = r.get(k)
            row.append(fmt_ms(v) if k.endswith("_ms") else "-" if v is None else str(v))
        cells.append(row)
    heads = [label if k == "key" else h for k, h in cols]
    widths = [max(len(heads[i]), *(len(r[i]) for r in cells), 0) for i in range(len(cols))]
    lines = [f"\n{title}", "  ".join(h.ljust(widths[0]) if i == 0 else h.rjust(widths[i]) for i, h in enumerate(heads))]
    for r in cells:
        lines.append("  ".join(c.ljust(widths[0]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(r)))
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more")
    return "\n".join(lines)


def fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def fmt_tok(v: int) -> str:
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if v >= div:
            return f"{v / div:.1f}{suffix}"
    return str(v)


COST_COLS = [
    ("key", "name"),
    ("usd", "usd"),
    ("input", "input"),
    ("output", "output"),
    ("cache_write", "cache_w"),
    ("cache_read", "cache_r"),
    ("n", "msgs"),
]


def cost_cells(r: dict) -> list[str]:
    return [
        str(r["key"]) if k == "key" else fmt_usd(r[k]) if k == "usd" else fmt_tok(r[k]) if k != "n" else str(r[k])
        for k, _ in COST_COLS
    ]


def render_cost_table(title: str, rows: list[dict], limit: int, label: str = "name") -> str:
    heads = [label if k == "key" else h for k, h in COST_COLS]
    cells = [cost_cells(r) for r in rows[:limit]]
    widths = [max(len(heads[i]), *(len(r[i]) for r in cells), 0) for i in range(len(heads))]
    lines = [f"\n{title}", "  ".join(h.ljust(widths[0]) if i == 0 else h.rjust(widths[i]) for i, h in enumerate(heads))]
    for r in cells:
        lines.append("  ".join(c.ljust(widths[0]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(r)))
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more")
    return "\n".join(lines)


def render_cost_text(cost: dict, limit: int, period: str) -> str:
    t = cost["total"]
    head = (
        f"\nCost (API-price estimate): {fmt_usd(t['usd'])}  input {fmt_tok(t['input'])}  output {fmt_tok(t['output'])}  "
        f"cache write {fmt_tok(t['cache_write'])}  cache read {fmt_tok(t['cache_read'])}  msgs {t['n']}"
    )
    parts = [
        head,
        render_cost_table("Cost by host", cost["host"], limit, "host"),
        render_cost_table("Cost by agent", cost["agent"], limit, "agent"),
        render_cost_table("Cost by model", cost["model"], limit, "model"),
        render_cost_table("Cost by project", cost["project"], limit, "project"),
        render_cost_table(f"Cost per {period}", cost[f"per_{period}"], limit, period),
    ]
    if cost["unpriced_models"]:
        parts.append("\nunpriced (tokens only): " + ", ".join(cost["unpriced_models"]))
    return "\n".join(parts)


def render_text(res: dict, limit: int, period: str) -> str:
    t = res["total"]
    parts = [
        f"hosts: {', '.join(res['hosts'])}",
        f"calls {t['n']}  errors {t['err']} ({t['err_pct']}%)  sessions {res['sessions']}  "
        f"mean {fmt_ms(t['mean_ms'])}  median {fmt_ms(t['median_ms'])}  p95 {fmt_ms(t['p95_ms'])}",
        render_table("By agent", res.get("agent", []), limit),
        render_table("By host", res.get("host", []), limit),
        render_table(f"Per {period}", res.get(f"per_{period}", []), limit),
        render_table("By tool", res.get("tool", []), limit),
        render_table("Bash by command", res.get("bash", []), limit, "command", False),
        render_table("By project", res.get("project", []), limit),
        render_table("By model", res.get("model", []), limit),
        render_table("Main vs subagent", res.get("side", []), limit, "context", False),
    ]
    parts.append(render_cost_text(res["cost"], limit, period))
    sp = res["spawns"]
    if sp:
        lines = ["\nSubagent spawns", "subagent_type  model  agent  spawns"]
        lines += [f"{r['subagent_type']}  {r['model']}  {r['agent']}  {r['n']}" for r in sp[:limit]]
        parts.append("\n".join(lines))
    return "\n".join(parts) + "\n"


HTML_CSS = """
:root{--bg:#fdf6e3;--fg:#5c6a72;--mut:#939f91;--card:#f4f0d9;--line:#e6e2cc;--bar:#8da101;--err:#f85552}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#2d353b;--fg:#d3c6aa;--mut:#859289;--card:#343f44;--line:#475258;--bar:#a7c080;--err:#e67e80}}
:root[data-theme=dark]{--bg:#2d353b;--fg:#d3c6aa;--mut:#859289;--card:#343f44;--line:#475258;--bar:#a7c080;--err:#e67e80}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}
main{max-width:980px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}
.sub{color:var(--mut);margin:0 0 16px}.kpis{display:flex;flex-wrap:wrap;gap:10px}
.kpi{background:var(--card);border-radius:8px;padding:10px 14px;min-width:110px}
.kpi b{display:block;font-size:20px}.kpi span{color:var(--mut);font-size:12px}
table{border-collapse:collapse;width:100%;background:var(--card);border-radius:8px;overflow:hidden}
th,td{padding:5px 10px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left;word-break:break-all}th{color:var(--mut);font-weight:500}
.wrap{overflow-x:auto}svg{display:block;width:100%;height:auto;background:var(--card);border-radius:8px}
svg text{fill:var(--mut);font-size:10px}
"""


def svg_bars(rows: list[dict], width: int = 940, height: int = 140, value: str = "n") -> str:
    if not rows:
        return ""
    mx = max(r[value] for r in rows) or 1
    bw = width / len(rows)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="calls over time">']
    for i, r in enumerate(rows):
        h = (height - 34) * r[value] / mx
        x = i * bw
        tip = (
            f"{r['key']}: {fmt_usd(r['usd'])}" if value == "usd" else f"{r['key']}: {r['n']} calls, {r['err_pct']}% err"
        )
        out.append(
            f'<rect x="{x + 1:.1f}" y="{height - 20 - h:.1f}" width="{max(bw - 2, 1):.1f}" height="{h:.1f}" '
            f'fill="var(--bar)" rx="2"><title>{html.escape(tip)}</title></rect>'
        )
        if len(rows) <= 24 or i % math.ceil(len(rows) / 12) == 0:
            out.append(
                f'<text x="{x + bw / 2:.1f}" y="{height - 6}" text-anchor="middle">{html.escape(r["key"][-5:])}</text>'
            )
    out.append("</svg>")
    return "".join(out)


def svg_hbars(rows: list[dict], width: int = 940) -> str:
    rows = rows[:12]
    if not rows:
        return ""
    mx = max(r["n"] for r in rows) or 1
    rh = 22
    out = [f'<svg viewBox="0 0 {width} {rh * len(rows) + 8}" role="img" aria-label="top tools">']
    for i, r in enumerate(rows):
        y = 6 + i * rh
        w = (width - 320) * r["n"] / mx
        out.append(f'<text x="8" y="{y + 13}">{html.escape(r["key"][:44])}</text>')
        out.append(f'<rect x="300" y="{y}" width="{max(w, 1):.1f}" height="16" fill="var(--bar)" rx="2"/>')
        out.append(f'<text x="{306 + w:.1f}" y="{y + 12}">{r["n"]}</text>')
    out.append("</svg>")
    return "".join(out)


def html_table(rows: list[dict], label: str, with_sessions: bool = True, limit: int = 30) -> str:
    cols = [c for c in COLS if with_sessions or c[0] != "sessions"]
    head = "".join(f"<th>{label if k == 'key' else h}</th>" for k, h in cols)
    body = []
    for r in rows[:limit]:
        tds = []
        for k, _ in cols:
            v = r.get(k)
            tds.append(f"<td>{html.escape(fmt_ms(v) if k.endswith('_ms') else '-' if v is None else str(v))}</td>")
        body.append(f"<tr>{''.join(tds)}</tr>")
    return f'<div class="wrap"><table><tr>{head}</tr>{"".join(body)}</table></div>'


def html_cost_table(rows: list[dict], label: str, limit: int = 30) -> str:
    head = "".join(f"<th>{label if k == 'key' else h}</th>" for k, h in COST_COLS)
    body = "".join(f"<tr>{''.join(f'<td>{html.escape(c)}</td>' for c in cost_cells(r))}</tr>" for r in rows[:limit])
    return f'<div class="wrap"><table><tr>{head}</tr>{body}</table></div>'


def render_html(res: dict, period: str) -> str:
    cost = res["cost"]
    t = res["total"]
    kpis = [
        ("calls", t["n"]),
        ("errors", f"{t['err_pct']}%"),
        ("sessions", res["sessions"]),
        ("mean", fmt_ms(t["mean_ms"])),
        ("p95", fmt_ms(t["p95_ms"])),
        ("subagent spawns", sum(r["n"] for r in res["spawns"])),
        ("est. cost", fmt_usd(cost["total"]["usd"])),
    ]
    k = "".join(f'<div class="kpi"><b>{v}</b><span>{n}</span></div>' for n, v in kpis)
    sections = [
        (f"Calls per {period}", svg_bars(res.get(f"per_{period}", []))),
        ("Top tools", svg_hbars(res.get("tool", []))),
        ("By agent", html_table(res.get("agent", []), "agent")),
        ("By host", html_table(res.get("host", []), "host")),
        ("By tool", html_table(res.get("tool", []), "tool")),
        ("Bash by command", html_table(res.get("bash", []), "command", False)),
        ("By project", html_table(res.get("project", []), "project")),
        ("By model", html_table(res.get("model", []), "model")),
        (f"Cost per {period} (API-price estimate)", svg_bars(cost[f"per_{period}"], value="usd")),
        ("Cost by host", html_cost_table(cost["host"], "host")),
        ("Cost by agent", html_cost_table(cost["agent"], "agent")),
        ("Cost by model", html_cost_table(cost["model"], "model")),
        ("Cost by project", html_cost_table(cost["project"], "project")),
    ]
    sp = res["spawns"]
    if sp:
        rows = "".join(
            f"<tr><td>{html.escape(r['subagent_type'])}</td><td>{html.escape(r['model'])}</td>"
            f"<td>{r['agent']}</td><td>{r['n']}</td></tr>"
            for r in sp[:30]
        )
        sections.append(
            (
                "Subagent spawns",
                f'<div class="wrap"><table><tr><th>subagent_type</th><th>model</th><th>agent</th><th>spawns</th></tr>{rows}</table></div>',
            )
        )
    body = "".join(f"<h2>{h}</h2>{c}" for h, c in sections if c)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Orca fleet stats</title><style>{HTML_CSS}</style></head><body><main>"
        f'<h1>Orca fleet stats</h1><p class="sub">hosts: {html.escape(", ".join(res["hosts"]))} - generated {res["generated"]}</p>'
        f'<div class="kpis">{k}</div>{body}</main></body></html>'
    )


def cmd_collect(a: argparse.Namespace) -> int:
    data = collect(parse_since(a.since), a.agent or AGENTS)
    if a.encode:
        sys.stdout.write(encode_payload(data, a.encode))
    else:
        json.dump(data, sys.stdout, separators=(",", ":"))
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    since = parse_since(a.since)
    agents = a.agent or list(AGENTS)
    try:
        targets = select_hosts(discover_hosts(), a.host or [])
    except HostError as e:
        print(f"orca-fleet-stats: {e}", file=sys.stderr)
        return 1
    hosts = []
    for host in targets:
        try:
            hosts.append(fetch_host(host, since, agents))
        except (HostError, OSError, ValueError) as e:
            print(f"orca-fleet-stats: host {host['name']} failed: {e}", file=sys.stderr)
    if not hosts:
        print("orca-fleet-stats: no host produced data", file=sys.stderr)
        return 1
    res = analyze(hosts, Filters(since, a.agent, a.project, a.tool))
    if a.html:
        Path(a.html).write_text(render_html(res, a.period))
        print(f"wrote {a.html}", file=sys.stderr)
    if a.json:
        json.dump(res, sys.stdout, indent=1)
        print()
    elif not a.html:
        sys.stdout.write(render_text(res, a.limit, a.period))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orca-fleet-stats", description="Activity statistics from local AI coding agent transcripts"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="emit a JSON summary of this machine")
    c.add_argument("--since")
    c.add_argument("--agent", action="append", choices=AGENTS)
    c.add_argument("--encode", metavar="NONCE", help=argparse.SUPPRESS)
    c.set_defaults(fn=cmd_collect)
    r = sub.add_parser("report", help="report across one or more hosts")
    r.add_argument(
        "--host", action="append", help="Orca environment name or 'local'; repeatable, default all Orca hosts"
    )
    r.add_argument("--since", help="YYYY-MM-DD or 7d / 4w / 3m")
    r.add_argument("--agent", action="append", choices=AGENTS)
    r.add_argument("--project", help="substring match")
    r.add_argument("--tool", help="substring match")
    r.add_argument("--period", choices=("day", "week", "month"), default="week")
    r.add_argument("--limit", type=int, default=15)
    r.add_argument("--json", action="store_true")
    r.add_argument("--html", metavar="FILE")
    r.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
