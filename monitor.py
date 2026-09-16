#!/usr/bin/env python3
"""
AI Session Monitor — a local, zero-dependency dashboard for your AI coding
agent sessions (Claude Code and Codex, extensible to more).

Shows every session found on this machine: how many are live, their context
rating (good / moderate / high / critical), how long they've been open, whether
they're stale, what each one is about, and the CPU/MEM of the live processes.

Usage:
    python3 monitor.py                 # serve dashboard at http://127.0.0.1:8787
    python3 monitor.py --port 9000     # custom port
    python3 monitor.py --once          # print a one-shot text summary and exit
    python3 monitor.py --json          # print the raw JSON snapshot and exit
    python3 monitor.py --clean --days 30           # dry-run: what would be archived
    python3 monitor.py --clean --days 30 --yes     # archive stale sessions

Requires only the Python 3 standard library. macOS/Linux.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Agents / storage layout
# ---------------------------------------------------------------------------

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")

# Each agent declares where transcripts live, where to archive them, the process
# basename that identifies a live CLI, and a colour for the UI badge.
AGENTS = {
    "claude": {
        "label": "Claude",
        "root": CLAUDE_PROJECTS,
        "archive": os.path.expanduser("~/.claude/projects-archive"),
        "proc": "claude",
        "color": "#d97757",
    },
    "codex": {
        "label": "Codex",
        "root": CODEX_SESSIONS,
        "archive": os.path.expanduser("~/.codex/sessions-archive"),
        "proc": "codex",
        "color": "#10a37f",
    },
}

DEFAULT_WINDOW = 200_000        # fallback context window when a session doesn't declare one
ACTIVE_SECS = 10 * 60
IDLE_SECS = 24 * 60 * 60
PID_MATCH_TOLERANCE = 8 * 60

_PARSE_CACHE = {}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _iso_to_epoch(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _base_session(path, agent):
    """Shared skeleton every parser fills in."""
    st = os.stat(path)
    return {
        "agent": agent,
        "session_id": None, "path": path, "title": None, "last_prompt": None,
        "cwd": None, "project": None, "branch": None, "model": None,
        "first_ts": None, "last_ts": None, "msg_count": 0,
        "context_tokens": 0, "total_output_tokens": 0,
        "window": None, "size_bytes": st.st_size, "is_subagent": False,
        "_mtime": st.st_mtime, "_ctime": st.st_ctime,
    }


def _finalize(s):
    if s["last_ts"] is None:
        s["last_ts"] = s["_mtime"]
    if s["first_ts"] is None:
        s["first_ts"] = s["_ctime"]
    if not s["session_id"]:
        s["session_id"] = os.path.splitext(os.path.basename(s["path"]))[0]
    if s["cwd"]:
        s["project"] = os.path.basename(s["cwd"])
    for k in ("_mtime", "_ctime"):
        s.pop(k, None)
    return s


# ---------------------------------------------------------------------------
# Claude parser
# ---------------------------------------------------------------------------

def parse_claude_session(path):
    s = _base_session(path, "claude")
    s["is_subagent"] = "/subagents/" in path
    latest_assistant = -1
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if d.get("sessionId") and not s["session_id"]:
                s["session_id"] = d["sessionId"]
            if t == "ai-title":
                s["title"] = d.get("aiTitle") or s["title"]; continue
            if t == "last-prompt":
                s["last_prompt"] = d.get("lastPrompt") or s["last_prompt"]; continue
            if t == "summary":
                s["title"] = s["title"] or d.get("summary"); continue
            ts = _iso_to_epoch(d.get("timestamp"))
            if ts:
                s["first_ts"] = ts if s["first_ts"] is None else min(s["first_ts"], ts)
                s["last_ts"] = ts if s["last_ts"] is None else max(s["last_ts"], ts)
            if d.get("cwd"):
                s["cwd"] = d["cwd"]
            if d.get("gitBranch"):
                s["branch"] = d["gitBranch"]
            if t == "user":
                s["msg_count"] += 1
            elif t == "assistant":
                s["msg_count"] += 1
                msg = d.get("message", {})
                if isinstance(msg, dict):
                    if msg.get("model"):
                        s["model"] = msg["model"]
                    u = msg.get("usage") or {}
                    s["total_output_tokens"] += u.get("output_tokens") or 0
                    if ts is not None and ts >= latest_assistant:
                        latest_assistant = ts
                        s["context_tokens"] = ((u.get("input_tokens") or 0)
                            + (u.get("cache_read_input_tokens") or 0)
                            + (u.get("cache_creation_input_tokens") or 0))
    return _finalize(s)


def _claude_text(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "thinking":
            th = (b.get("thinking") or "").strip()
            if th:
                parts.append("💭 " + th)
        elif t == "tool_use":
            parts.append(f"[→ tool: {b.get('name', '?')}]")
        elif t == "tool_result":
            c = b.get("content")
            parts.append("[tool result] " + (_claude_text(c) if isinstance(c, (list, str)) else "")[:200])
        elif t == "image":
            parts.append("[image]")
    return "\n".join(p for p in parts if p).strip()


def claude_tail(path, n):
    turns = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") not in ("user", "assistant"):
                continue
            msg = d.get("message", {})
            if not isinstance(msg, dict):
                continue
            text = _claude_text(msg.get("content"))
            if text:
                turns.append({"role": d["type"], "text": text, "ts": _iso_to_epoch(d.get("timestamp"))})
    return turns[-n:]


# ---------------------------------------------------------------------------
# Codex parser
# ---------------------------------------------------------------------------

def _codex_text(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"):
            out.append(b.get("text", ""))
    return "\n".join(p for p in out if p).strip()


def _codex_is_real_user(text):
    # skip injected developer/environment/permission blocks
    return text and not text.lstrip().startswith("<")


def parse_codex_session(path):
    s = _base_session(path, "codex")
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            p = d.get("payload", {}) if isinstance(d.get("payload"), dict) else {}
            ts = _iso_to_epoch(d.get("timestamp"))
            if ts:
                s["first_ts"] = ts if s["first_ts"] is None else min(s["first_ts"], ts)
                s["last_ts"] = ts if s["last_ts"] is None else max(s["last_ts"], ts)
            if t == "session_meta":
                s["session_id"] = p.get("id") or s["session_id"]
                s["cwd"] = p.get("cwd") or s["cwd"]
            elif t == "turn_context":
                s["cwd"] = p.get("cwd") or s["cwd"]
                s["model"] = p.get("model") or s["model"]
            elif t == "event_msg":
                pt = p.get("type")
                if pt == "thread_name_updated":
                    s["title"] = p.get("thread_name") or s["title"]
                elif pt == "task_started" and p.get("model_context_window"):
                    s["window"] = p["model_context_window"]
                elif pt == "token_count":
                    info = p.get("info") or {}
                    last = info.get("last_token_usage") or {}
                    tot = info.get("total_token_usage") or {}
                    if last.get("total_tokens"):
                        s["context_tokens"] = last["total_tokens"]
                    if tot.get("output_tokens"):
                        s["total_output_tokens"] = tot["output_tokens"]
                    if info.get("model_context_window"):
                        s["window"] = info["model_context_window"]
            elif t == "response_item" and p.get("type") == "message":
                role = p.get("role")
                if role in ("user", "assistant"):
                    s["msg_count"] += 1
                    if role == "user":
                        txt = _codex_text(p.get("content"))
                        if _codex_is_real_user(txt):
                            s["last_prompt"] = txt
                            if not s["title"]:
                                s["title"] = txt[:60]
    return _finalize(s)


def codex_tail(path, n):
    turns = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "response_item":
                continue
            p = d.get("payload", {})
            if not isinstance(p, dict) or p.get("type") != "message":
                continue
            role = p.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _codex_text(p.get("content"))
            if role == "user" and not _codex_is_real_user(text):
                continue
            if text:
                turns.append({"role": role, "text": text, "ts": _iso_to_epoch(d.get("timestamp"))})
    return turns[-n:]


# ---------------------------------------------------------------------------
# Discovery (all agents)
# ---------------------------------------------------------------------------

_PARSERS = {"claude": parse_claude_session, "codex": parse_codex_session}
_TAILERS = {"claude": claude_tail, "codex": codex_tail}


def _agent_for_path(path):
    real = os.path.realpath(path)
    for name, a in AGENTS.items():
        root = os.path.realpath(a["root"])
        if real == root or real.startswith(root + os.sep):
            return name
    return None


def _iter_transcripts():
    if os.path.isdir(CLAUDE_PROJECTS):
        for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True):
            if "/subagents/" in path or os.sep + "memory" + os.sep in path:
                continue
            yield "claude", path
    if os.path.isdir(CODEX_SESSIONS):
        for path in glob.glob(os.path.join(CODEX_SESSIONS, "**", "rollout-*.jsonl"), recursive=True):
            yield "codex", path


def discover_sessions():
    sessions = []
    for agent, path in _iter_transcripts():
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        key = (path, st.st_mtime)
        cached = _PARSE_CACHE.get(key)
        if cached is None:
            try:
                cached = _PARSERS[agent](path)
            except Exception:
                continue
            _PARSE_CACHE[key] = cached
        sessions.append(cached)
    return sessions


# ---------------------------------------------------------------------------
# Live processes
# ---------------------------------------------------------------------------

def live_processes():
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,%cpu=,%mem=,rss=,lstart=,command="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    wanted = {a["proc"]: name for name, a in AGENTS.items()}
    procs = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(.{24})\s+(.*)$", line)
        if not m:
            continue
        pid, ppid, cpu, mem, rss, lstart, command = m.groups()
        cmd = command.strip()
        argv0 = cmd.split()[0] if cmd.split() else ""
        base = os.path.basename(argv0)
        if base not in wanted:
            continue
        try:
            start_epoch = time.mktime(time.strptime(lstart.strip(), "%a %b %d %H:%M:%S %Y"))
        except Exception:
            start_epoch = None
        procs.append({
            "pid": int(pid), "agent": wanted[base], "cpu": float(cpu),
            "mem": float(mem), "rss_mb": round(int(rss) / 1024, 1),
            "start_epoch": start_epoch, "command": cmd,
        })
    return procs


def match_pids_to_sessions(sessions, procs):
    for s in sessions:
        s["pid"] = s["cpu"] = s["mem"] = s["rss_mb"] = None
    candidates = []
    for p in procs:
        if p["start_epoch"] is None:
            continue
        for s in sessions:
            if s["is_subagent"] or s["first_ts"] is None or s["agent"] != p["agent"]:
                continue
            delta = abs(s["first_ts"] - p["start_epoch"])
            if delta <= PID_MATCH_TOLERANCE:
                candidates.append((delta, p, s))
    candidates.sort(key=lambda c: c[0])
    used_pids, used_sessions = set(), set()
    for delta, p, s in candidates:
        if p["pid"] in used_pids or id(s) in used_sessions:
            continue
        used_pids.add(p["pid"]); used_sessions.add(id(s))
        s["pid"], s["cpu"], s["mem"], s["rss_mb"] = p["pid"], p["cpu"], p["mem"], p["rss_mb"]
    return len(used_pids)


# ---------------------------------------------------------------------------
# Clearing / archiving (safe, reversible by default)
# ---------------------------------------------------------------------------

def _safe_transcript_path(path):
    """Validate a transcript path or raise ValueError. Returns (real, agent, root)."""
    real = os.path.realpath(path)
    if not real.endswith(".jsonl"):
        raise ValueError("refusing: not a .jsonl transcript")
    low = (os.sep + os.path.relpath(real, "/") + os.sep).lower()
    if os.sep + "memory" + os.sep in low or os.sep + "memories" + os.sep in low:
        raise ValueError("refusing: inside a memory folder")
    for name, a in AGENTS.items():
        root = os.path.realpath(a["root"])
        if real == root or real.startswith(root + os.sep):
            return real, name, root
    raise ValueError("path is not inside a known agent session folder")


def clear_session(path, purge=False):
    real, agent, root = _safe_transcript_path(path)
    if not os.path.exists(real):
        raise ValueError("file not found")
    targets = [real]
    sidecar = real[:-len(".jsonl")]
    if os.path.isdir(sidecar):
        targets.append(sidecar)
    archive_root = AGENTS[agent]["archive"]
    moved = []
    for t in targets:
        trel = os.path.relpath(t, root)
        if purge:
            shutil.rmtree(t, ignore_errors=True) if os.path.isdir(t) else os.remove(t)
        else:
            dest = os.path.join(archive_root, trel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                dest += "." + str(int(time.time()))
            os.replace(t, dest)
        moved.append(trel)
    return {"ok": True, "purged": purge, "agent": agent, "items": moved,
            "archive_dir": None if purge else archive_root}


def _clear_many(victims, purge=False, dry_run=True):
    res = {"ok": True, "purge": purge, "dry_run": dry_run, "count": len(victims),
           "cleared": 0, "failed": [], "items": []}
    for s in victims:
        e = {"agent": s["agent"], "title": s["title"] or s["session_id"][:8],
             "age_days": round(s["age_secs"] / 86400, 1), "path": s["path"],
             "size_mb": round(s["size_bytes"] / 1048576, 2)}
        if not dry_run:
            try:
                clear_session(s["path"], purge=purge); e["cleared"] = True; res["cleared"] += 1
            except Exception as ex:
                e["error"] = str(ex); res["failed"].append(e)
        res["items"].append(e)
    return res


def clean_stale(days, purge=False, dry_run=True):
    """CLI: archive every non-live session untouched for more than `days`."""
    snap = build_snapshot()
    cutoff = days * 86400
    victims = [s for s in snap["sessions"] if not s["is_live"] and s["age_secs"] > cutoff]
    res = _clear_many(victims, purge=purge, dry_run=dry_run)
    res["days"] = days
    return res


def clear_stale_sessions(agent=None, purge=False, dry_run=False, default_window=DEFAULT_WINDOW):
    """Dashboard: archive every session currently in the `stale` state (optionally one agent).

    One snapshot, one loop, per-session error reporting — the browser used to fire one
    request per session, which gave no progress feedback and stopped silently if the tab
    reloaded mid-way.
    """
    snap = build_snapshot(default_window)
    victims = [s for s in snap["sessions"]
               if s["state"] == "stale" and not s["is_live"] and (agent is None or s["agent"] == agent)]
    return _clear_many(victims, purge=purge, dry_run=dry_run)


def get_tail(path, n):
    _safe_transcript_path(path)
    agent = _agent_for_path(path)
    return _TAILERS.get(agent, claude_tail)(path, n)


# ---------------------------------------------------------------------------
# Ratings & snapshot
# ---------------------------------------------------------------------------

def context_rating(tokens, window):
    pct = (tokens / window) if window else 0
    if pct < 0.50:
        return "good", pct
    if pct < 0.75:
        return "moderate", pct
    if pct < 0.90:
        return "high", pct
    return "critical", pct


def staleness(last_ts, is_live, now):
    if is_live:
        return "live"
    age = now - last_ts
    if age <= ACTIVE_SECS:
        return "active"
    if age <= IDLE_SECS:
        return "idle"
    return "stale"


def build_snapshot(default_window=DEFAULT_WINDOW):
    now = time.time()
    sessions = [dict(s) for s in discover_sessions()]   # copy: we mutate per-request fields
    procs = live_processes()
    live_count = match_pids_to_sessions(sessions, procs)
    for s in sessions:
        win = s.get("window") or default_window
        rating, pct = context_rating(s["context_tokens"], win)
        s["window"] = win
        s["context_rating"] = rating
        s["context_pct"] = round(pct * 100, 1)
        s["is_live"] = s["pid"] is not None
        s["state"] = staleness(s["last_ts"], s["is_live"], now)
        s["age_secs"] = round(now - s["last_ts"])
        s["open_secs"] = round(now - s["first_ts"]) if s["first_ts"] else None
    sessions.sort(key=lambda s: (not s["is_live"], -s["last_ts"]))
    by_agent = {}
    for name in AGENTS:
        by_agent[name] = sum(1 for s in sessions if s["agent"] == name)
    return {
        "generated_at": now, "default_window": default_window,
        "live_process_count": len(procs), "matched_live_sessions": live_count,
        "total_cpu": round(sum(p["cpu"] for p in procs), 1),
        "total_rss_mb": round(sum(p["rss_mb"] for p in procs), 1),
        "agents": {name: {"label": a["label"], "color": a["color"]} for name, a in AGENTS.items()},
        "by_agent": by_agent,
        "counts": {
            "total": len(sessions),
            "live": sum(1 for s in sessions if s["state"] == "live"),
            "active": sum(1 for s in sessions if s["state"] == "active"),
            "idle": sum(1 for s in sessions if s["state"] == "idle"),
            "stale": sum(1 for s in sessions if s["state"] == "stale"),
        },
        "sessions": sessions,
    }


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

def human_age(secs):
    if secs is None:
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


def print_once(snap):
    c = snap["counts"]
    agents = " ".join(f"{k}:{v}" for k, v in snap["by_agent"].items())
    print(f"\n  AI Sessions — {datetime.now().strftime('%H:%M:%S')}   [{agents}]")
    print(f"  live procs: {snap['live_process_count']}  |  matched: {snap['matched_live_sessions']}"
          f"  |  active: {c['active']}  idle: {c['idle']}  stale: {c['stale']}  total: {c['total']}")
    print(f"  aggregate  CPU {snap['total_cpu']}%   RSS {snap['total_rss_mb']} MB\n")
    print(f"  {'AGENT':<7}{'STATE':<7} {'CTX':>5} {'RATING':<9} {'OPEN':>7} {'IDLE':>6} {'PID':>6}  TITLE")
    print("  " + "-" * 96)
    for s in snap["sessions"]:
        if s["state"] == "stale":
            continue
        title = (s["title"] or (s["last_prompt"] or "")[:60] or s["session_id"][:8])[:48]
        print(f"  {s['agent']:<7}{s['state']:<7} {s['context_pct']:>4.0f}% {s['context_rating']:<9} "
              f"{human_age(s['open_secs']):>7} {human_age(s['age_secs']):>6} "
              f"{str(s['pid'] or '-'):>6}  {title}")
    print()


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Session Monitor</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2b333f;
    --text:#e6edf3; --muted:#8b949e;
    --good:#3fb950; --moderate:#d29922; --high:#db8f2f; --critical:#f85149;
    --live:#3fb950; --active:#58a6ff; --idle:#8b949e; --stale:#6e7681;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:20px; flex-wrap:wrap; position:sticky; top:0;
    background:var(--bg); z-index:10; }
  h1 { font-size:16px; margin:0; font-weight:600; }
  .stats { display:flex; gap:18px; flex-wrap:wrap; margin-left:auto; align-items:center; }
  .stat { text-align:center; }
  .stat .n { font-size:20px; font-weight:700; }
  .stat .l { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; }
  .controls { display:flex; gap:8px; align-items:center; padding:12px 24px;
    border-bottom:1px solid var(--border); flex-wrap:wrap; }
  .controls input[type=text] { background:var(--panel); border:1px solid var(--border);
    color:var(--text); padding:6px 10px; border-radius:6px; min-width:220px; }
  .chip { background:var(--panel); border:1px solid var(--border); color:var(--muted);
    padding:5px 12px; border-radius:16px; cursor:pointer; font-size:12px; user-select:none; }
  .chip.on { background:var(--active); color:#fff; border-color:var(--active); }
  .chip.busy { opacity:.55; pointer-events:none; }
  .muted { color:var(--muted); font-size:12px; }
  #grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
    gap:14px; padding:20px 24px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:14px; border-left:4px solid var(--idle); position:relative; }
  .card.live { border-left-color:var(--live); }
  .card.active { border-left-color:var(--active); }
  .card.idle { border-left-color:var(--idle); }
  .card.stale { border-left-color:var(--stale); opacity:.72; }
  .card h2 { font-size:14px; margin:0 0 4px; font-weight:600; }
  .badges { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; align-items:center; }
  .b { font-size:10px; padding:2px 7px; border-radius:10px; font-weight:600;
    text-transform:uppercase; letter-spacing:.3px; }
  .b.live{background:rgba(63,185,80,.18);color:var(--live);}
  .b.active{background:rgba(88,166,255,.18);color:var(--active);}
  .b.idle{background:rgba(139,148,158,.18);color:var(--idle);}
  .b.stale{background:rgba(110,118,129,.18);color:var(--stale);}
  .b.proj{background:var(--panel2);color:var(--text);}
  .b.agent{color:#fff;}
  .prompt { color:var(--muted); font-size:12px; margin:6px 0 10px; max-height:52px; overflow:hidden; }
  .bar { height:8px; background:var(--panel2); border-radius:4px; overflow:hidden; margin:4px 0; }
  .bar > div { height:100%; border-radius:4px; }
  .fill-good{background:var(--good);} .fill-moderate{background:var(--moderate);}
  .fill-high{background:var(--high);} .fill-critical{background:var(--critical);}
  .ctx-row { display:flex; justify-content:space-between; font-size:11px; color:var(--muted); }
  .meta { display:grid; grid-template-columns:1fr 1fr; gap:4px 12px; font-size:12px; margin-top:10px; color:var(--muted); }
  .meta b { color:var(--text); font-weight:600; }
  .rating-good{color:var(--good);} .rating-moderate{color:var(--moderate);}
  .rating-high{color:var(--high);} .rating-critical{color:var(--critical);}
  .actions { display:flex; gap:6px; margin-top:10px; }
  .resume,.clear { font-size:11px; border:1px solid var(--border); border-radius:6px; padding:5px 8px;
    background:var(--panel2); cursor:pointer; user-select:none;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .resume { flex:1; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .resume:hover { border-color:var(--active); color:var(--text); }
  .resume.copied { border-color:var(--good); color:var(--good); }
  .clear { color:var(--muted); white-space:nowrap; }
  .clear:hover { border-color:var(--critical); color:var(--critical); }
  .card.removing { opacity:.3; transform:scale(.97); transition:.25s; }
  .summ-btn { margin-top:8px; font-size:11px; color:var(--muted); cursor:pointer; text-align:center;
    border:1px solid var(--border); border-radius:6px; padding:5px 8px; background:var(--panel2); user-select:none; }
  .summ-btn:hover { border-color:var(--active); color:var(--text); }
  .summ { margin-top:8px; border-top:1px dashed var(--border); padding-top:8px; max-height:320px; overflow-y:auto; display:none; }
  .summ.open { display:block; }
  .turn { margin-bottom:8px; font-size:12px; line-height:1.45; }
  .turn .who { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.4px;
    display:inline-block; padding:1px 6px; border-radius:8px; margin-bottom:2px; }
  .turn.user .who { background:rgba(88,166,255,.18); color:var(--active); }
  .turn.assistant .who { background:rgba(63,185,80,.14); color:var(--good); }
  .turn .body { color:var(--text); white-space:pre-wrap; word-break:break-word; }
  .turn .body.think { color:var(--muted); font-style:italic; }
  /* ---- list view ---- */
  #grid.list { display:flex; flex-direction:column; gap:4px; padding:12px 16px; }
  .row { position:relative; display:flex; align-items:center; gap:10px; flex-wrap:wrap;
    background:var(--panel); border:1px solid var(--border); border-left:3px solid var(--idle);
    border-radius:6px; padding:5px 10px; font-size:12px; }
  .row.live{border-left-color:var(--live);} .row.active{border-left-color:var(--active);}
  .row.idle{border-left-color:var(--idle);} .row.stale{border-left-color:var(--stale);opacity:.72;}
  .row.removing{opacity:.3;transform:scale(.99);transition:.25s;}
  .row .dot{ width:7px;height:7px;border-radius:50%;flex:0 0 auto; }
  .row .rtitle{ flex:1 1 180px; min-width:110px; font-weight:600; color:var(--text);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row .rproj{ color:var(--muted); font-size:11px; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; max-width:150px; flex:0 1 auto; }
  .row .rbar{ width:70px; height:6px; background:var(--panel2); border-radius:3px; overflow:hidden; flex:0 0 auto; }
  .row .rbar > span{ display:block; height:100%; }
  .row .rpct{ width:38px; text-align:right; flex:0 0 auto; font-variant-numeric:tabular-nums; }
  .row .rtime{ color:var(--muted); white-space:nowrap; flex:0 0 auto;
    font-variant-numeric:tabular-nums; min-width:60px; }
  .row .rowact{ position:absolute; right:6px; top:4px; display:none; gap:5px;
    background:var(--panel2); border:1px solid var(--border); border-radius:6px; padding:2px 4px; }
  .row:hover .rowact{ display:flex; }
  .row .resume.mini,.row .clear.mini,.row .summ-btn.mini{ padding:2px 6px; font-size:11px; margin-top:0; }
  .row .summ{ flex-basis:100%; }
  /* ---- mini / corner-widget mode (layers on top of list view) ---- */
  #miniTog { font-size:14px; line-height:1; }
  #miniCount { display:none; font-size:12px; font-weight:600; }
  #miniCount .dot { display:inline-block; width:7px; height:7px; border-radius:50%;
    background:var(--live); margin-right:4px; vertical-align:middle; }
  body.mini header, body.mini footer { display:none; }
  body.mini .controls { padding:5px 8px; gap:6px; }
  body.mini .controls > *:not(#miniTog):not(#miniCount) { display:none; }
  body.mini #miniCount { display:inline-block; margin-left:auto; }
  body.mini #grid.list { padding:5px 7px; gap:2px; }
  body.mini .row { padding:3px 8px; gap:8px; border-radius:4px; font-size:11px; }
  body.mini .row .rproj, body.mini .row .rbar, body.mini .row .ropen { display:none; }
  body.mini .row .rtitle { flex:1 1 auto; min-width:0; }
  body.mini .row .rpct { width:34px; }
  body.mini .row .ridle { min-width:0; }
  body.mini .row .rowact { top:2px; right:5px; }
  footer { padding:10px 24px; color:var(--muted); font-size:11px; border-top:1px solid var(--border); }
  code { background:var(--panel2); padding:1px 5px; border-radius:4px; font-size:11px; }
</style>
</head>
<body>
<header>
  <h1>🛰️ AI Session Monitor</h1>
  <div class="stats" id="stats"></div>
</header>
<div class="controls">
  <input type="text" id="search" placeholder="Filter by title, project, branch, prompt…">
  <span class="chip viewtog" data-v="tile" title="Tile view">▦</span>
  <span class="chip viewtog" data-v="list" title="List view">☰</span>
  <span class="chip" id="miniTog" title="Mini / corner-widget mode">⊟</span>
  <span class="chip on" data-f="all">All</span>
  <span class="chip" data-f="live">Live</span>
  <span class="chip" data-f="active">Active</span>
  <span class="chip" data-f="idle">Idle</span>
  <span class="chip" data-f="stale">Stale</span>
  <span id="agentChips"></span>
  <span class="chip" id="clearAll" style="border-color:var(--critical);color:var(--critical)">🗑 Clear all stale</span>
  <span class="muted" id="updated" style="margin-left:auto"></span>
  <span id="miniCount"></span>
</div>
<div id="grid"></div>
<footer id="foot"></footer>
<script>
let DATA=null, FILTER="all", AGENTF="all", Q="", VIEW=localStorage.getItem("ai-monitor-view")||"tile";
let MINI=localStorage.getItem("ai-monitor-mini")==="1";
const OPEN=new Set(), SUMM={};
const fmtAge=s=>{ if(s==null)return "?"; if(s<60)return s+"s"; if(s<3600)return Math.floor(s/60)+"m";
  if(s<86400)return Math.floor(s/3600)+"h"+Math.floor((s%3600)/60)+"m"; return Math.floor(s/86400)+"d"+Math.floor((s%86400)/3600)+"h"; };
const fmtTok=n=>n>=1000?(n/1000).toFixed(1)+"k":String(n);
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function resumeCmd(s){ const cd=s.cwd?`cd ${s.cwd} && `:"";
  return s.agent==="codex" ? `${cd}codex resume ${s.session_id}` : `${cd}claude --resume ${s.session_id}`; }

async function refresh(){
  try{ const r=await fetch("/api/sessions"); DATA=await r.json(); render(); }
  catch(e){ document.getElementById("foot").textContent="⚠ "+e; }
}
function render(){
  if(!DATA) return;
  const c=DATA.counts;
  const agentStats=Object.entries(DATA.by_agent).map(([k,v])=>[k,v]);
  document.getElementById("stats").innerHTML=[
    ["live procs",DATA.live_process_count],["matched",DATA.matched_live_sessions],
    ["active",c.active],["idle",c.idle],["stale",c.stale],["total",c.total],
    ["cpu",DATA.total_cpu+"%"],["mem",DATA.total_rss_mb+"mb"]
  ].map(([l,n])=>`<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
  document.getElementById("updated").textContent="updated "+new Date(DATA.generated_at*1000).toLocaleTimeString();
  document.getElementById("miniCount").innerHTML=`<span class="dot"></span>${c.live} live`;

  // agent filter chips (only if >1 agent has sessions)
  const ac=document.getElementById("agentChips");
  if(!ac.dataset.built){
    const active=Object.entries(DATA.by_agent).filter(([k,v])=>v>0);
    if(active.length>1){
      ac.innerHTML=active.map(([k,v])=>`<span class="chip agentf" data-a="${k}" style="border-color:${DATA.agents[k].color}">${DATA.agents[k].label} ${v}</span>`).join("");
      ac.querySelectorAll(".agentf").forEach(ch=>ch.onclick=()=>{
        const was=ch.classList.contains("on");
        ac.querySelectorAll(".agentf").forEach(x=>x.classList.remove("on"));
        if(was){AGENTF="all";}else{ch.classList.add("on");AGENTF=ch.dataset.a;} render();
      });
    }
    ac.dataset.built="1";
  }

  const grid=document.getElementById("grid"); grid.innerHTML="";
  grid.className = (VIEW==="list"||MINI) ? "list" : "";
  const multiAgent = Object.values(DATA.by_agent).filter(v=>v>0).length>1;
  let shown=0;
  for(const s of DATA.sessions){
    if(MINI && s.state!=="live" && s.state!=="active") continue;
    if(FILTER!=="all" && s.state!==FILTER) continue;
    if(AGENTF!=="all" && s.agent!==AGENTF) continue;
    if(Q){ const hay=((s.title||"")+" "+(s.project||"")+" "+(s.branch||"")+" "+(s.last_prompt||"")+" "+s.agent).toLowerCase();
      if(!hay.includes(Q)) continue; }
    shown++;
    const title=esc(s.title || (s.last_prompt||"").slice(0,60) || s.session_id.slice(0,8));
    const ag=DATA.agents[s.agent]||{label:s.agent,color:"#666"};
    const pid=s.pid?`pid ${s.pid}`:"";
    const cpu=s.cpu!=null?`${s.cpu}% cpu`:""; const mem=s.rss_mb!=null?`${s.rss_mb}mb`:"";
    if(VIEW==="list"){
      grid.insertAdjacentHTML("beforeend",`
        <div class="row ${s.state}">
          ${multiAgent?`<span class="dot" style="background:${ag.color}" title="${esc(ag.label)}"></span>`:""}
          <span class="rtitle" title="${title}">${title}</span>
          ${s.project?`<span class="rproj">${esc(s.project)}</span>`:""}
          <span class="rbar"><span class="fill-${s.context_rating}" style="width:${Math.min(100,s.context_pct)}%"></span></span>
          <span class="rpct rating-${s.context_rating}" title="${fmtTok(s.context_tokens)}/${fmtTok(s.window)} tok">${s.context_pct}%</span>
          <span class="rtime ropen" title="open (first activity)">open ${fmtAge(s.open_secs)}</span>
          <span class="rtime ridle" title="idle (since last activity)">idle ${fmtAge(s.age_secs)}</span>
          <span class="rowact">
            <div class="resume mini" data-cmd="${esc(resumeCmd(s))}" title="copy resume command"><span class="txt">⧉</span></div>
            ${s.state==="live"?"":`<div class="clear mini" data-path="${esc(s.path)}" data-title="${esc(s.title||s.session_id.slice(0,8))}" title="archive this session (recoverable)">🗑</div>`}
            <span class="summ-btn mini" data-path="${esc(s.path)}" title="show summary">▾</span>
          </span>
          <div class="summ"></div>
        </div>`);
      continue;
    }
    grid.insertAdjacentHTML("beforeend",`
      <div class="card ${s.state}">
        <div class="badges">
          <span class="b agent" style="background:${ag.color}">${esc(ag.label)}</span>
          <span class="b ${s.state}">${s.state}</span>
          ${s.project?`<span class="b proj">${esc(s.project)}</span>`:""}
          ${s.branch&&s.branch!=="HEAD"?`<span class="b proj">⎇ ${esc(s.branch)}</span>`:""}
        </div>
        <h2>${title}</h2>
        <div class="prompt">${esc((s.last_prompt||"").slice(0,160))}</div>
        <div class="ctx-row"><span>context</span>
          <span class="rating-${s.context_rating}">${s.context_pct}% · ${s.context_rating} · ${fmtTok(s.context_tokens)}/${fmtTok(s.window)} tok</span></div>
        <div class="bar"><div class="fill-${s.context_rating}" style="width:${Math.min(100,s.context_pct)}%"></div></div>
        <div class="meta">
          <div>open <b>${fmtAge(s.open_secs)}</b></div>
          <div>idle <b>${fmtAge(s.age_secs)}</b></div>
          <div>msgs <b>${s.msg_count}</b></div>
          <div>model <b>${esc((s.model||"?").replace("claude-",""))}</b></div>
          ${pid?`<div>${pid}</div>`:"<div></div>"}
          <div>${[cpu,mem].filter(Boolean).join(" · ")}</div>
        </div>
        <div class="actions">
          <div class="resume" data-cmd="${esc(resumeCmd(s))}" title="click to copy resume command">
            <span class="ico">⧉</span><span class="txt">resume ${esc(s.session_id.slice(0,8))}…</span>
          </div>
          ${s.state==="live"?"":`<div class="clear" data-path="${esc(s.path)}" data-title="${esc(s.title||s.session_id.slice(0,8))}" title="archive this session (recoverable)">🗑 clear</div>`}
        </div>
        <div class="summ-btn" data-path="${esc(s.path)}">▾ show summary</div>
        <div class="summ"></div>
      </div>`);
  }
  grid.querySelectorAll(".summ-btn").forEach(sb=>{
    if(OPEN.has(sb.dataset.path)&&SUMM[sb.dataset.path]){
      const p=(sb.closest(".card,.row")||document).querySelector(".summ");
      p.innerHTML=SUMM[sb.dataset.path]; p.classList.add("open");
      sb.textContent=sb.classList.contains("mini")?"▴":"▴ hide summary";
    }
  });
  if(!shown) grid.innerHTML=`<div class="muted" style="padding:${MINI?'10px':'20px'}">${MINI?"No live or active sessions.":"No sessions match this filter."}</div>`;
  document.getElementById("foot").innerHTML=`Reading <code>~/.claude/projects</code> + <code>~/.codex/sessions</code> · auto-refresh 5s · showing ${shown}/${c.total}`;
}
document.getElementById("grid").addEventListener("click",async e=>{
  const sb=e.target.closest(".summ-btn");
  if(sb){
    const path=sb.dataset.path, panel=(sb.closest(".card,.row")||document).querySelector(".summ"), mini=sb.classList.contains("mini");
    const L={show:mini?"▾":"▾ show summary", hide:mini?"▴":"▴ hide summary", load:mini?"…":"loading…"};
    if(panel.classList.contains("open")){ panel.classList.remove("open"); sb.textContent=L.show; OPEN.delete(path); return; }
    sb.textContent=L.load; OPEN.add(path);
    try{
      const r=await fetch("/api/tail?n=6&path="+encodeURIComponent(path)); const j=await r.json();
      let html;
      if(!j.ok) html=`<div class="muted">${esc(j.error||"error")}</div>`;
      else if(!j.messages.length) html=`<div class="muted">No message content found.</div>`;
      else html=j.messages.map(m=>{ const think=m.text.startsWith("💭");
        const body=m.text.length>600?m.text.slice(0,600)+"…":m.text;
        return `<div class="turn ${m.role}"><span class="who">${m.role}</span><div class="body${think?' think':''}">${esc(body)}</div></div>`; }).join("");
      SUMM[path]=html; panel.innerHTML=html; panel.classList.add("open"); sb.textContent=L.hide;
    }catch(err){ panel.innerHTML=`<div class="muted">${esc(String(err))}</div>`; panel.classList.add("open"); sb.textContent=L.hide; }
    return;
  }
  const clr=e.target.closest(".clear");
  if(clr){
    if(!confirm(`Archive this session?\n\n"${clr.dataset.title}"\n\nMoves to the agent's session-archive folder (recoverable). Memories and code are untouched.`)) return;
    const card=clr.closest(".card,.row"); card.classList.add("removing");
    try{
      const r=await fetch("/api/clear",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:clr.dataset.path})});
      const j=await r.json();
      if(!j.ok){ card.classList.remove("removing"); alert("Could not clear: "+(j.error||"unknown")); return; }
      setTimeout(refresh,300);
    }catch(err){ card.classList.remove("removing"); alert("Error: "+err); }
    return;
  }
  const r=e.target.closest(".resume"); if(!r) return;
  const cmd=r.dataset.cmd, txt=r.querySelector(".txt"), orig=txt.textContent;
  const done=()=>{ r.classList.add("copied"); txt.textContent="copied ✓"; setTimeout(()=>{ r.classList.remove("copied"); txt.textContent=orig; },1400); };
  if(navigator.clipboard&&window.isSecureContext) navigator.clipboard.writeText(cmd).then(done).catch(()=>fb(cmd,done)); else fb(cmd,done);
});
function fb(cmd,done){ const t=document.createElement("textarea"); t.value=cmd; t.style.position="fixed"; t.style.opacity="0";
  document.body.appendChild(t); t.select(); try{ document.execCommand("copy"); done(); }catch(e){ prompt("Copy this:",cmd); } document.body.removeChild(t); }
let CLEARING=false;
async function clearAllStale(){
  if(CLEARING) return;
  const stale=DATA.sessions.filter(s=>s.state==="stale"&&(AGENTF==="all"||s.agent===AGENTF));
  if(!stale.length){ alert("No stale sessions to clear."); return; }
  const who=AGENTF==="all"?"":` ${(DATA.agents[AGENTF]||{label:AGENTF}).label}`;
  if(!confirm(`Archive ALL ${stale.length}${who} stale sessions (untouched >24h)?\n\nRecoverable. Live/idle sessions, memories, and code are untouched.`)) return;
  const btn=document.getElementById("clearAll"), orig=btn.textContent;
  CLEARING=true; btn.classList.add("busy"); btn.textContent=`⏳ archiving ${stale.length}…`;
  try{
    const r=await fetch("/api/clear-stale",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({agent:AGENTF==="all"?null:AGENTF})});
    const j=await r.json();
    if(!j.ok) throw new Error(j.error||"unknown error");
    let msg=`Archived ${j.cleared} of ${j.count} stale session(s).`;
    if(j.failed.length){
      msg+=`\n\n${j.failed.length} could not be archived:\n`+j.failed.slice(0,8).map(f=>`• ${f.title}: ${f.error}`).join("\n");
      if(j.failed.length>8) msg+=`\n…and ${j.failed.length-8} more`;
    }
    alert(msg);
  }catch(err){ alert("Clear all stale failed: "+err.message); }
  finally{ CLEARING=false; btn.classList.remove("busy"); btn.textContent=orig; refresh(); }
}
document.querySelectorAll(".chip[data-f]").forEach(ch=>ch.onclick=()=>{
  document.querySelectorAll(".chip[data-f]").forEach(x=>x.classList.remove("on"));
  ch.classList.add("on"); FILTER=ch.dataset.f; render();
});
document.getElementById("clearAll").onclick=clearAllStale;
document.getElementById("search").oninput=e=>{ Q=e.target.value.toLowerCase().trim(); render(); };
document.querySelectorAll(".viewtog").forEach(ch=>{
  ch.classList.toggle("on", ch.dataset.v===VIEW);
  ch.onclick=()=>{ VIEW=ch.dataset.v; localStorage.setItem("ai-monitor-view",VIEW);
    document.querySelectorAll(".viewtog").forEach(x=>x.classList.toggle("on",x.dataset.v===VIEW)); render(); };
});
const miniTog=document.getElementById("miniTog");
function applyMini(){ document.body.classList.toggle("mini",MINI); miniTog.classList.toggle("on",MINI); }
miniTog.onclick=()=>{ MINI=!MINI; localStorage.setItem("ai-monitor-mini",MINI?"1":"0"); applyMini(); render(); };
applyMini();
refresh(); setInterval(refresh,5000);
</script>
</body>
</html>"""


def make_handler(window):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/sessions"):
                self._send(200, json.dumps(build_snapshot(window)).encode(), "application/json")
            elif self.path.startswith("/api/tail"):
                qs = parse_qs(urlparse(self.path).query)
                path = (qs.get("path") or [""])[0]
                try:
                    n = int((qs.get("n") or ["6"])[0])
                except Exception:
                    n = 6
                try:
                    tail = get_tail(path, max(1, min(n, 30)))
                    self._send(200, json.dumps({"ok": True, "messages": tail}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(200, HTML_PAGE.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path not in ("/api/clear", "/api/clear-stale"):
                self._send(404, b"not found", "text/plain"); return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "bad request"}).encode(), "application/json"); return
            if self.path == "/api/clear-stale":
                agent = body.get("agent") or None
                if agent is not None and agent not in AGENTS:
                    self._send(400, json.dumps({"ok": False, "error": f"unknown agent {agent!r}"}).encode(), "application/json"); return
                try:
                    res = clear_stale_sessions(agent=agent, purge=bool(body.get("purge")),
                                               dry_run=bool(body.get("dry_run")), default_window=window)
                    self._send(200, json.dumps(res).encode(), "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
                return
            snap = build_snapshot(window)
            match = next((s for s in snap["sessions"] if s["path"] == body.get("path")), None)
            if match and match["is_live"]:
                self._send(409, json.dumps({"ok": False, "error": "session is live; close it first"}).encode(), "application/json"); return
            try:
                self._send(200, json.dumps(clear_session(body.get("path"), purge=bool(body.get("purge")))).encode(), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
    return Handler


def serve(port, host, window):
    httpd = ThreadingHTTPServer((host, port), make_handler(window))
    print(f"\n  AI Session Monitor running at  http://{host}:{port}")
    print(f"  Watching {CLAUDE_PROJECTS}")
    print(f"       and {CODEX_SESSIONS}")
    print("  Ctrl-C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")


def main():
    ap = argparse.ArgumentParser(description="Local dashboard for AI coding agent sessions (Claude, Codex).")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help="fallback context window for sessions that don't declare one (default 200000)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--clean", action="store_true", help="archive stale sessions untouched > --days")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--yes", action="store_true", help="actually perform --clean (otherwise dry-run)")
    ap.add_argument("--purge", action="store_true", help="delete permanently instead of archiving")
    args = ap.parse_args()

    if not os.path.isdir(CLAUDE_PROJECTS) and not os.path.isdir(CODEX_SESSIONS):
        print("error: found neither ~/.claude/projects nor ~/.codex/sessions", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(build_snapshot(args.window), indent=2)); return
    if args.once:
        print_once(build_snapshot(args.window)); return
    if args.clean:
        dry = not args.yes
        res = clean_stale(args.days, purge=args.purge, dry_run=dry)
        verb = "PURGE" if args.purge else "archive"
        print(f"\n  {'DRY-RUN — would ' if dry else ''}{verb} {res['count']} session(s) untouched > {args.days}d:\n")
        total = 0.0
        for it in res["items"]:
            total += it["size_mb"]
            mark = "" if dry else (" ✓" if it.get("cleared") else f" ✗ {it.get('error','')}")
            print(f"    {it['agent']:<7}{it['age_days']:>5.1f}d {it['size_mb']:>6.2f}MB  {(it['title'] or '')[:46]}{mark}")
        print(f"\n  total: {res['count']} session(s), {total:.1f} MB")
        print(f"  (dry-run — re-run with --yes)\n" if dry else "  done.\n")
        return
    serve(args.port, args.host, args.window)


if __name__ == "__main__":
    main()
