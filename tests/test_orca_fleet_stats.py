import json
import sqlite3
from pathlib import Path

import orca_fleet_stats as a
import pytest

FIX = Path(__file__).parent / "fixtures"


def test_norm_tool():
    assert a.norm_tool("claude", "mcp__plugin_foo_bar__do_it") == "mcp:foo_bar/do_it"
    assert a.norm_tool("claude", "Task") == "Agent"
    assert a.norm_tool("codex", "exec_command") == "Bash"
    assert a.norm_tool("opencode", "task") == "Agent"


def test_bash_word():
    assert a.bash_word("cd /x && git status") == "git"
    assert a.bash_word("FOO=1 sudo /usr/bin/rg x") == "rg"
    assert a.bash_word("") == ""


def test_project_name():
    assert a.project_name("/home/u/Repos/Worktrees/app/feat") == "app"
    assert a.project_name("/home/u/Repos/app") == "app"
    assert a.project_name(None, "fb") == "fb"


def test_claude_parser(tmp_path):
    proj = tmp_path / "-home-u-Repos-app"
    proj.mkdir()
    (proj / "s1.jsonl").write_text((FIX / "claude.jsonl").read_text())
    calls = {c["raw"]: c for c in a.claude_calls(root=tmp_path)}
    assert set(calls) == {"Bash", "mcp__plugin_foo_bar__do_it", "Task", "Read"}
    assert calls["Bash"]["sub"] == "git"
    assert calls["Bash"]["lat"] == 2.0
    assert calls["mcp__plugin_foo_bar__do_it"]["err"] is True
    assert calls["Task"]["spawn"] == ("Explore", "sonnet")
    assert calls["Task"]["lat"] is None
    assert calls["Read"]["side"] is True
    assert calls["Bash"]["project"] == "app"
    assert calls["Bash"]["model"] == "claude-opus-x"


def test_claude_dedup(tmp_path):
    for n in ("a", "b"):
        (tmp_path / f"{n}.jsonl").write_text((FIX / "claude.jsonl").read_text())
    assert len(list(a.claude_calls(root=tmp_path))) == 4


def test_codex_parser(tmp_path):
    (tmp_path / "r.jsonl").write_text((FIX / "codex.jsonl").read_text())
    calls = list(a.codex_calls(root=tmp_path))
    assert [c["sub"] for c in calls] == ["rg", "ls"]
    assert [c["err"] for c in calls] == [True, False]
    assert calls[0]["lat"] == 1.0
    assert calls[0]["project"] == "app"
    assert calls[0]["model"] == "gpt-x"


def test_opencode_parser(tmp_path):
    db = tmp_path / "o.db"
    con = sqlite3.connect(db)
    con.executescript(
        "create table session(id, directory, parent_id);"
        "create table message(id, data);"
        "create table part(session_id, message_id, data, time_created);"
    )
    con.execute("insert into session values('s','/home/u/app',null)")
    con.execute("insert into message values('m',?)", (json.dumps({"role": "assistant", "modelID": "mm"}),))
    part = {
        "type": "tool",
        "tool": "bash",
        "state": {"status": "error", "input": {"command": "git x"}, "time": {"start": 1000, "end": 3000}},
    }
    con.execute("insert into part values('s','m',?,1000)", (json.dumps(part),))
    con.commit()
    con.close()
    (c,) = list(a.opencode_calls(db=db))
    assert (c["tool"], c["sub"], c["err"], c["lat"], c["model"]) == ("Bash", "git", True, 2.0, "mm")


def test_collect_and_analyze(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "s1.jsonl").write_text((FIX / "claude.jsonl").read_text())
    data = a.collect_from({"claude": lambda since: a.claude_calls(since, tmp_path)}, None)
    data["label"] = "h"
    res = a.analyze([data], a.Filters())
    assert res["total"]["n"] == 4
    assert res["total"]["err"] == 1
    assert res["sessions"] == 1
    assert res["spawns"][0]["subagent_type"] == "Explore"
    only = a.analyze([data], a.Filters(tool="bash"))
    assert only["total"]["n"] == 1
    assert a.analyze([data], a.Filters(project="nope"))["total"]["n"] == 0
    assert "Orca fleet stats" in a.render_html(res, "week")
    assert "By tool" in a.render_text(res, 5, "week")


def test_quantile():
    hist = {str(a.lat_bucket(100)): 9, str(a.lat_bucket(10000)): 1}
    median = a.quantile(hist, 0.5)
    top = a.quantile(hist, 1.0)
    assert median is not None and abs(median - 100) < 25
    assert top is not None and abs(top - 10000) < 2500


def test_parse_since():
    assert a.parse_since("2026-01-02") == "2026-01-02"
    assert a.parse_since(None) is None


HOST_LIST = {
    "hosts": [
        {"kind": "local", "name": "this machine", "id": "local", "machineName": "fedora"},
        {"kind": "environment", "name": "thanatos", "id": "abc"},
        {"kind": "ssh", "name": "box", "id": "ssh1"},
    ]
}


def test_parse_hosts_keeps_local_and_environments():
    hosts = a.parse_hosts(HOST_LIST)
    assert [(h["name"], h["label"]) for h in hosts] == [("local", "fedora"), ("thanatos", "thanatos")]


def test_select_hosts():
    hosts = a.parse_hosts(HOST_LIST)
    assert a.select_hosts(hosts, []) == hosts
    assert [h["name"] for h in a.select_hosts(hosts, ["thanatos", "thanatos"])] == ["thanatos"]
    with pytest.raises(a.HostError, match="unknown host nope"):
        a.select_hosts(hosts, ["nope"])


def test_pick_worktree_prefers_main_and_skips_archived():
    result = {
        "worktrees": [
            {"id": "w1", "isMainWorktree": False},
            {"id": "w2", "isMainWorktree": True, "isArchived": True},
            {"id": "w3", "isMainWorktree": True},
        ]
    }
    assert a.pick_worktree(result) == "w3"
    with pytest.raises(a.HostError):
        a.pick_worktree({"worktrees": []})


def _terminal_lines(data: dict, nonce: str) -> list[str]:
    noise = ["$ stty -echo", f"{a.SENTINEL}_BEGIN {nonce}-other", "junk"]
    return [*noise, *a.encode_payload(data, nonce).splitlines(), "$ "]


def test_payload_roundtrip_through_terminal_noise():
    data = {"host": "orca", "buckets": [{"n": i, "tool": "Bash" * 20} for i in range(200)]}
    lines = [ln + "\r" for ln in _terminal_lines(data, "abc123")]
    assert len(lines) > 10
    assert a.find_payload(lines, "abc123") == data


def test_payload_incomplete_returns_none():
    lines = _terminal_lines({"x": 1}, "n1")[:-3]
    assert a.find_payload(lines, "n1") is None


def test_payload_checksum_and_count_mismatch():
    lines = _terminal_lines({"x": list(range(500))}, "n2")
    i = next(k for k, ln in enumerate(lines) if ln == f"{a.SENTINEL}_BEGIN n2") + 1
    corrupt = lines[:]
    corrupt[i] = "A" + corrupt[i][1:]
    with pytest.raises(a.HostError, match="checksum"):
        a.find_payload(corrupt, "n2")
    with pytest.raises(a.HostError, match="incomplete"):
        a.find_payload([ln for k, ln in enumerate(lines) if k != i], "n2")


def test_payload_fail_marker():
    with pytest.raises(a.HostError, match="collector failed"):
        a.find_payload([f"{a.SENTINEL}_FAIL n3"], "n3")


def test_echoed_command_is_not_a_sentinel():
    assert not a.has_line([f'echo "{a.SENTINEL}_""READY n"'], f"{a.SENTINEL}_READY n")
    assert a.has_line([f"{a.SENTINEL}_READY n\r"], f"{a.SENTINEL}_READY n")


def test_upload_lines_reassemble_to_script():
    import base64
    import gzip

    script = b"print('hi')\n" * 5000
    lines = a.upload_lines(script)
    b64 = "".join(ln.split("'")[1] for ln in lines)
    assert gzip.decompress(base64.b64decode(b64)) == script
    assert all(len(ln) < 1000 for ln in lines)


def test_orca_reports_error_message(monkeypatch):
    class R:
        stdout = b'{"ok": false, "error": {"message": "boom"}}'
        stderr = b""
        returncode = 1

    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kw: R())
    with pytest.raises(a.HostError, match="boom"):
        a.orca(["host", "list"])


def _claude_usage_line(mid, rid, model="claude-sonnet-4-6", ts="2026-10-02T09:00:00+00:00", **extra):
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 2000,
        "cache_read_input_tokens": 10000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1000},
        "server_tool_use": {"web_search_requests": 2},
    }
    return json.dumps(
        {
            "timestamp": ts,
            "requestId": rid,
            "cwd": "/home/u/Repos/Worktrees/app/feat",
            "message": {"id": mid, "model": model, "usage": usage},
            **extra,
        }
    )


def test_price_for_normalization():
    assert a.price_for("claude-sonnet-4-6") == (3, 15, 3.75, 6, 0.3)
    assert a.price_for("claude-opus-4-5-20251101") == a.price_for("claude-opus-4-5")
    assert a.price_for("claude-haiku-4-5-20990101") == a.price_for("claude-haiku-4-5")
    assert a.price_for("gpt-x") is None


def test_usage_cost_splits_cache_ttl_and_searches():
    usage = json.loads(_claude_usage_line("m", "r"))["message"]["usage"]
    price = a.price_for("claude-sonnet-4-6")
    assert price is not None
    expected = (1000 * 3 + 500 * 15 + 1000 * 3.75 + 1000 * 6 + 10000 * 0.3) / 1e6 + 0.02
    assert a.usage_cost(usage, price) == pytest.approx(expected)


def test_claude_usage_parse_and_dedup(tmp_path):
    sub = tmp_path / "-home-u-Repos-app" / "s1" / "subagents"
    sub.mkdir(parents=True)
    lines = [
        _claude_usage_line("m1", "r1"),
        _claude_usage_line("m1", "r1"),
        _claude_usage_line("m2", "r2", isSidechain=True),
        _claude_usage_line("m3", "r3", model="<synthetic>"),
        _claude_usage_line("m4", "r4", model="mystery"),
        "not json",
        json.dumps({"message": {"role": "user", "content": "usage"}}),
    ]
    (tmp_path / "-home-u-Repos-app" / "s1.jsonl").write_text("\n".join(lines))
    (sub / "agent.jsonl").write_text(_claude_usage_line("m1", "r1") + "\n" + _claude_usage_line("m5", "r5"))
    rows = list(a.claude_usage(root=tmp_path))
    assert len(rows) == 4
    priced = [r for r in rows if r["model"] == "claude-sonnet-4-6"]
    assert len(priced) == 3
    assert priced[0]["project"] == "app"
    assert (priced[0]["input"], priced[0]["output"], priced[0]["cache_write"], priced[0]["cache_read"]) == (
        1000,
        500,
        2000,
        10000,
    )
    assert priced[0]["usd"] == pytest.approx(0.04325)
    assert sum(r["side"] for r in rows) == 1
    mystery = next(r for r in rows if r["model"] == "mystery")
    assert mystery["usd"] == 0.0 and mystery["input"] == 1000


def test_usage_project_git_root(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "pkg" / "deep").mkdir(parents=True)
    assert a.usage_project(str(tmp_path / "repo" / "pkg" / "deep")) == "repo"
    assert a.usage_project("/home/u/Repos/Worktrees/app/feat") == "app"
    assert a.usage_project("/home/u/Repos/app/.claude/worktrees/agent-1") == "app"
    assert a.usage_project("") == "~"


def test_codex_usage_uses_deltas(tmp_path):
    def tc(ts, i, c, o):
        total = {"input_tokens": i, "cached_input_tokens": c, "cache_write_input_tokens": 0, "output_tokens": o}
        return json.dumps(
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": total}},
            }
        )

    lines = [
        (FIX / "codex.jsonl").read_text().splitlines()[0],
        (FIX / "codex.jsonl").read_text().splitlines()[1],
        tc("2026-10-02T09:01:00.000Z", 100, 40, 10),
        tc("2026-10-02T09:01:00.000Z", 100, 40, 10),
        tc("2026-10-02T09:02:00.000Z", 300, 100, 30),
    ]
    (tmp_path / "r.jsonl").write_text("\n".join(lines))
    rows = list(a.codex_usage(root=tmp_path))
    assert [(r["input"], r["cache_read"], r["output"]) for r in rows] == [(60, 40, 10), (140, 60, 20)]
    assert rows[0]["project"] == "app" and rows[0]["model"] == "gpt-x" and rows[0]["usd"] == 0.0


def test_opencode_usage(tmp_path):
    db = tmp_path / "o.db"
    con = sqlite3.connect(db)
    con.executescript("create table session(id, directory, parent_id);create table message(id, session_id, data);")
    con.execute("insert into session values('s','/home/u/app',null)")
    msg = {
        "role": "assistant",
        "modelID": "claude-sonnet-4.6",
        "time": {"created": 1790000000000},
        "tokens": {"input": 100, "output": 50, "reasoning": 0, "cache": {"read": 1000, "write": 200}},
    }
    con.execute("insert into message values('m','s',?)", (json.dumps(msg),))
    con.execute("insert into message values('u','s',?)", (json.dumps({"role": "user"}),))
    con.commit()
    con.close()
    (r,) = list(a.opencode_usage(db=db))
    assert r["model"] == "claude-sonnet-4-6"
    assert (r["input"], r["output"], r["cache_write"], r["cache_read"]) == (100, 50, 200, 1000)
    assert r["usd"] == pytest.approx((100 * 3 + 50 * 15 + 200 * 3.75 + 1000 * 0.3) / 1e6)


def test_cost_collect_merge_and_filters(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "s.jsonl").write_text(_claude_usage_line("m1", "r1") + "\n" + _claude_usage_line("m2", "r2"))
    src = {"claude": lambda since: a.claude_usage(since, tmp_path)}
    h1 = a.collect_from({}, None, src)
    h1["label"] = "h1"
    h2 = json.loads(json.dumps(h1))
    h2["label"] = "h2"
    cost = a.analyze([h1, h2], a.Filters())["cost"]
    one = a.analyze([h1], a.Filters())["cost"]["total"]
    assert one["n"] == 2 and one["input"] == 2000
    assert cost["total"]["usd"] == pytest.approx(2 * one["usd"])
    assert [r["key"] for r in cost["host"]] == ["h1", "h2"] or [r["key"] for r in cost["host"]] == ["h2", "h1"]
    assert cost["model"][0]["key"] == "claude-sonnet-4-6"
    assert cost["project"][0]["key"] == "app"
    assert cost["per_day"][0]["key"] == "2026-10-02"
    assert a.analyze([h1], a.Filters(project="nope"))["cost"]["total"]["usd"] == 0
    assert a.analyze([h1], a.Filters(since="2026-10-03"))["cost"]["total"]["n"] == 0
    assert a.analyze([h1], a.Filters(agents=["codex"]))["cost"]["total"]["n"] == 0
    res = a.analyze([h1, h2], a.Filters())
    assert "Cost (API-price estimate)" in a.render_text(res, 5, "week")
    assert "Cost by model" in a.render_html(res, "week")
