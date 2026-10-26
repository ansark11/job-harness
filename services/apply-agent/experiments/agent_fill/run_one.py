#!/usr/bin/env python3
"""One agent run in its own process, so the parent can hard-kill it if it hangs.

    ./python/bin/python run_one.py job.json result.json

job.json: {"task", "cdp_url", "resume", "max_steps", "time_limit"}. Writes result.json with
the agent's report, usage, and every action the safety guard refused.
"""

import asyncio
import json
import sys
from pathlib import Path

import harness


async def main(job_path, result_path):
    job = json.loads(Path(job_path).read_text())
    refusals = []
    try:
        result = await harness.run_agent(job["task"], job["cdp_url"], job["resume"], refusals,
                                         max_steps=job.get("max_steps", harness.MAX_STEPS),
                                         time_limit=job.get("time_limit", harness.TIME_LIMIT))
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"[:500]}
    harness.dump(Path(result_path), {**result, "refusals": refusals})


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
