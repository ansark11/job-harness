"""Structured run logging, shared format across the harness.

IDENTICAL COPIES of this file live in every service and workflow
(services do not import each other; scripts/test.sh fails if the copies
drift). The master is scripts/runlog.py: edit it, then copy it to the others.

Each run of a CLI appends JSON lines to <component>/logs/<component>.jsonl:

  {"ts", "trace_id", "run_id", "parent_run_id", "component", "cmd",
   "code_version", "event", "level", "data"}

  run_id         one invocation of one tool
  trace_id       one end-to-end job; a workflow passes its own down to every
                 tool it calls, so one trace id reconstructs everything it did.
                 A tool run standalone is its own trace.
  parent_run_id  the run that launched this one, if any

Correlation travels through environment variables (HARNESS_TRACE_ID,
HARNESS_PARENT_RUN_ID); use Run.child_env() when launching a subprocess.
HARNESS_LOG_DIR redirects every component's log (tests use this).

Secrets are never written: values under keys that look like credentials or
session cookies, and strings shaped like OpenAI keys or LinkedIn li_at
cookies, are replaced before writing.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 10_000_000      # rotate at ~10 MB
KEEP = 3                    # <name>.jsonl.1 .. .3

SECRET_KEY = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|credential|cookie|li_at|jsessionid|csrf)", re.I)
SECRET_VALUE = re.compile(r"^sk-[A-Za-z0-9_-]{8,}|^AQED[A-Za-z0-9_-]{20,}")   # OpenAI keys; LinkedIn li_at cookies
REDACTED = "[redacted]"
ALLOWED_TOKEN_KEYS = {"input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "cached_tokens",
                      "cache_write_tokens", "usage"}


def redact(obj):
    if isinstance(obj, dict):
        return {k: (REDACTED if SECRET_KEY.search(str(k)) and str(k) not in ALLOWED_TOKEN_KEYS and
                    not isinstance(v, (dict, list)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and SECRET_VALUE.search(obj):
        return REDACTED
    return obj


def digest(data):
    """Short stable fingerprint of bytes/str/JSON-able data, to tie outputs to inputs."""
    if not isinstance(data, (bytes, str)):
        data = json.dumps(data, sort_keys=True, default=str)
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()[:16]


_VERSION = None


def code_version(start):
    global _VERSION
    if _VERSION is None:
        try:
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=start, capture_output=True,
                                 text=True, timeout=5)
            sha = out.stdout.strip() or "unknown"
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=start,
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            _VERSION = sha + ("+dirty" if dirty else "")
        except (OSError, subprocess.SubprocessError):
            _VERSION = "unknown"
    return _VERSION


class Run:
    def __init__(self, component, base_dir, argv=None):
        self.component = component
        self.base_dir = Path(base_dir)
        self.run_id = uuid.uuid4().hex[:12]
        self.trace_id = os.environ.get("HARNESS_TRACE_ID") or self.run_id
        self.parent_run_id = os.environ.get("HARNESS_PARENT_RUN_ID") or None
        self.cmd = redact([Path(sys.argv[0]).name] + list(sys.argv[1:] if argv is None else argv))
        log_dir = Path(os.environ["HARNESS_LOG_DIR"]) if os.environ.get("HARNESS_LOG_DIR") else self.base_dir / "logs"
        self.path = log_dir / f"{component}.jsonl"
        self.version = code_version(self.base_dir)
        self._t0 = time.monotonic()
        self.finished = False

    def child_env(self, base=None):
        """Environment for a subprocess so its log records join this trace."""
        env = dict(os.environ if base is None else base)
        env["HARNESS_TRACE_ID"] = self.trace_id
        env["HARNESS_PARENT_RUN_ID"] = self.run_id
        return env

    def _write(self, event, level, data):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "trace_id": self.trace_id,
               "run_id": self.run_id, "parent_run_id": self.parent_run_id, "component": self.component,
               "cmd": self.cmd, "code_version": self.version, "event": event, "level": level,
               "data": redact(data)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                for i in range(KEEP - 1, 0, -1):
                    older = self.path.with_name(f"{self.path.name}.{i}")
                    if older.exists():
                        older.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
                self.path.replace(self.path.with_name(f"{self.path.name}.1"))
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass   # logging must never break the tool itself

    def event(self, name, **data):
        self._write(name, "info", data)

    def warn(self, name, **data):
        self._write(name, "warning", data)

    def error(self, name, **data):
        self._write(name, "error", data)

    def finish(self, exit_code, **data):
        if not self.finished:
            self.finished = True
            self._write("run_end", "info" if exit_code == 0 else "error",
                        {"exit_code": exit_code, "duration_ms": round((time.monotonic() - self._t0) * 1000), **data})

    # context manager: logs run_start, and run_end or a crash with its traceback
    def __enter__(self):
        self._write("run_start", "info", {"python": sys.version.split()[0], "cwd": os.getcwd()})
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and not issubclass(exc_type, SystemExit):
            self._write("crash", "error", {"exception": f"{exc_type.__name__}: {exc}",
                                           "traceback": "".join(traceback.format_exception(exc_type, exc, tb))[-8000:]})
            self.finish(1)
        elif exc_type is SystemExit:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            self.finish(code)
        return False


def start(component, base_dir, argv=None):
    return Run(component, base_dir, argv)
