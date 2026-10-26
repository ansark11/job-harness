"""Safety harness for the agent-fill experiment (docs/agent-fill-experiment.md, Step 3).

One browser, two drivers: Playwright launches Chromium (temp profile, a debugging port)
and owns the network blocker; browser-use attaches to the same browser over CDP and drives
it. Every page in that browser, including tabs the agent opens, goes through the blocker.

Layers, outermost first:
  1. NetworkBlocker: aborts every request that could send data (anything but GET/HEAD/
     OPTIONS), except search/autocomplete lookups and GraphQL queries. Logs each one; one
     whose URL or body looks like a submission marks the run attempted_submit.
  2. Action guard: browser-use's click / input / send_keys / select actions are wrapped;
     a click on anything named like submit / sign in / create account / pay, or typing
     into a password field, is refused and logged.
  3. Limits: MAX_STEPS steps (the main cap) and TIME_LIMIT seconds per posting, and each
     run is its own child process, hard-killed at HARD_LIMIT if it hangs.
Errors: a configuration error from the model (bad request, bad key, unknown model) stops the
run at once; temporary errors and page trouble get browser-use's normal retries. A run that
spans a laptop sleep is flagged "interrupted" so it can be rerun rather than counted.

Never submits, never signs in, never creates an account, never uses real data.
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))      # services/apply-agent: bench.py, forms.py (read-only reuse)

import bench  # noqa: E402

MAX_STEPS = 40
TIME_LIMIT = 600          # 10 min: a 20-30 field form plus navigation at 10-20 s a step
GRACE = 120               # the child process is killed this long after TIME_LIMIT
MODEL = "gpt-6-luna"
# Errors retrying can't fix: stop at once instead of burning six retries per step.
CONFIG_ERROR = re.compile(r"Error code: (400|401|403|404)\b|invalid_request_error|invalid_api_key|"
                          r"model_not_found|unsupported_value|insufficient_quota", re.I)
SLEEP_GAP = 30            # wall clock running this far ahead of the run's own clock = the Mac slept

GUARDED = re.compile(r"submit|send application|sign.?in|log.?in|create (an )?account|register|\bpay\b|purchase",
                     re.I)
SUBMISSION = re.compile(r"submit|apply|application|candidate|/jobs?/[^?]*/(app|form)|/form", re.I)


def api_key():
    for line in (HERE / ".env").read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("OPENAI_API_KEY missing from experiments/agent_fill/.env")


def keep_awake():
    """caffeinate for as long as this process lives (the lid still sleeps the Mac)."""
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except OSError:
        return None


def llm():
    from browser_use import ChatOpenAI
    # gpt-6-luna only accepts its default temperature; browser-use would send 0.2 and 0.3.
    return ChatOpenAI(model=MODEL, api_key=api_key(), temperature=None, frequency_penalty=None)


async def preflight(model):
    """One tiny call before a run: a configuration error shows up here in seconds."""
    from browser_use.llm.messages import UserMessage
    try:
        await model.ainvoke([UserMessage(content="Reply with OK.")])
    except Exception as e:
        if CONFIG_ERROR.search(str(e)):
            return str(e)[:400]
        raise
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NetworkBlocker:
    """Playwright route handler for the whole context; armed for the whole run."""

    def __init__(self):
        self.blocked = []

    def looks_like_submission(self, url, body):
        return bool(SUBMISSION.search(url) or SUBMISSION.search(body[:2000]))

    async def __call__(self, route):
        req = route.request
        if req.method in ("GET", "HEAD", "OPTIONS") or bench.NO_DATA_OUT.search(req.url):
            return await route.continue_()
        try:
            body = (req.post_data_buffer or b"").decode("utf-8", "replace")
        except Exception:
            body = ""
        squashed = body.replace(" ", "")
        if '"query":"query' in squashed or '"query":"{' in squashed:       # GraphQL read, not a write
            return await route.continue_()
        self.blocked.append({"method": req.method, "url": req.url[:300], "body": body[:300],
                             "submission": self.looks_like_submission(req.url, body), "at": time.time()})
        return await route.abort()

    @property
    def attempted_submit(self):
        return any(b["submission"] for b in self.blocked)


async def launch(headless=True):
    """(playwright, context, cdp_url, blocker): a browser the agent can attach to, all traffic routed."""
    from playwright.async_api import async_playwright
    port = free_port()
    pw = await async_playwright().start()
    profile = tempfile.mkdtemp(prefix="agent-fill-profile-")
    ctx = await pw.chromium.launch_persistent_context(
        profile, headless=headless, viewport={"width": 1280, "height": 900},
        args=[f"--remote-debugging-port={port}"])
    blocker = NetworkBlocker()
    await ctx.route("**/*", blocker)
    return pw, ctx, f"http://127.0.0.1:{port}", blocker


def guard_tools(refusals, enabled=True):
    """browser-use Tools with click/input/send_keys/select wrapped by the action guard.
    enabled=False (safety test only) leaves the network blocker as the only layer."""
    from browser_use import Tools
    from browser_use.agent.views import ActionResult
    tools = Tools()
    if not enabled:
        tools.set_coordinate_clicking(False)
        return tools
    actions = tools.registry.registry.actions

    async def describe(browser_session, index):
        node = await browser_session.get_element_by_index(index) if index is not None else None
        if node is None:
            return None, ""
        attrs = node.attributes or {}
        text = " ".join(filter(None, [node.get_all_children_text()[:200], attrs.get("aria-label"), attrs.get("value"),
                                      attrs.get("title"), attrs.get("name"), attrs.get("id")]))
        return node, text

    def wrap(name, check):
        original = actions[name].function

        async def guarded(*, params, browser_session=None, **kw):
            reason = await check(params, browser_session)
            if reason:
                refusals.append({"action": name, "reason": reason, "at": time.time()})
                return ActionResult(error=f"Refused by the safety guard: {reason}. Do not submit, sign in, "
                                          f"or create an account; continue filling or stop and report.")
            return await original(params=params, browser_session=browser_session, **kw)
        actions[name].function = guarded

    async def check_click(params, session):
        node, text = await describe(session, getattr(params, "index", None))
        if getattr(params, "index", None) is None:
            return "clicking by screen position is disabled in this experiment"
        if node is not None and GUARDED.search(text):
            return f"click on {text[:80]!r}"
        return None

    async def check_input(params, session):
        node, text = await describe(session, getattr(params, "index", None))
        if node is not None and (node.attributes or {}).get("type", "").lower() == "password":
            return "typing into a password field"
        return None

    async def check_keys(params, session):
        return None          # Enter can submit a form; the network blocker is the backstop for that

    for name, check in (("click", check_click), ("input", check_input), ("send_keys", check_keys)):
        if name in actions:
            wrap(name, check)
    tools.set_coordinate_clicking(False)
    return tools


async def run_agent(task, cdp_url, resume_path, refusals, max_steps=MAX_STEPS, time_limit=TIME_LIMIT, guard=True):
    """Run browser-use on the attached browser. Returns a dict of what happened.
    Normally called inside a child process (run_one.py) via run_isolated()."""
    from browser_use import Agent, Browser
    model = llm()
    config_error = await preflight(model)
    if config_error:
        return {"final_report": None, "done": False, "steps": 0, "seconds": 0, "timed_out": False,
                "config_error": config_error, "tokens_in": None, "tokens_out": None, "cost_usd": None, "urls": []}
    browser = Browser(cdp_url=cdp_url, keep_alive=True)
    agent = Agent(task=task, llm=model, browser=browser, tools=guard_tools(refusals, enabled=guard),
                  available_file_paths=[str(resume_path)], calculate_cost=True)
    stop_reason = []

    async def stop_on_config_error(a):
        for r in (a.state.last_result or []):
            if r.error and CONFIG_ERROR.search(r.error):
                stop_reason.append(r.error[:400])
                a.state.stopped = True

    t0 = time.monotonic()
    timed_out = False
    try:
        history = await asyncio.wait_for(agent.run(max_steps=max_steps, on_step_end=stop_on_config_error),
                                         timeout=time_limit)
    except asyncio.TimeoutError:
        timed_out = True
        history = agent.history
    usage = history.usage
    return {"final_report": history.final_result(), "done": history.is_done(), "steps": history.number_of_steps(),
            "seconds": round(time.monotonic() - t0, 1), "timed_out": timed_out,
            "config_error": stop_reason[0] if stop_reason else None,
            "tokens_in": usage.total_prompt_tokens if usage else None,
            "tokens_out": usage.total_completion_tokens if usage else None,
            "cost_usd": round(usage.total_cost, 4) if usage else None,
            "urls": [u for u in history.urls() if u][-5:] if hasattr(history, "urls") else []}


async def run_isolated(job, time_limit=TIME_LIMIT, grace=GRACE):
    """Run one agent job in a child process (run_one.py) and hard-kill it if it hangs.

    The parent's event loop keeps serving the network blocker while the child drives the
    browser over CDP. Returns the child's result plus hard_killed and interrupted flags."""
    work = Path(tempfile.mkdtemp(prefix="agent-fill-job-"))
    job_path, result_path = work / "job.json", work / "result.json"
    dump(job_path, {**job, "time_limit": time_limit})
    wall0, mono0 = time.time(), time.monotonic()
    proc = await asyncio.create_subprocess_exec(sys.executable, str(HERE / "run_one.py"), str(job_path),
                                                str(result_path))
    hard_killed = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=time_limit + grace)
    except asyncio.TimeoutError:
        hard_killed = True
        proc.kill()
        await proc.wait()
    slept = (time.time() - wall0) - (time.monotonic() - mono0) > SLEEP_GAP
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    return {**result, "hard_killed": hard_killed, "interrupted": slept, "exit_code": proc.returncode}


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n")
