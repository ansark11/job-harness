"""OpenAI Responses API calls with structured (JSON schema) output.

Same pattern as finance-harness card-offers/discover.py: stdlib urllib, key
from the environment or this service's .env, and an injectable `post` so
tests run on canned replies and never reach the network.
"""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "gpt-6-luna"
API_URL = "https://api.openai.com/v1/responses"
ENV_PATH = Path(__file__).resolve().parent / ".env"


class ModelError(Exception):
    pass


def api_key():
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.strip().startswith("OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    raise ModelError(f"OPENAI_API_KEY not set (add it to {ENV_PATH})")


def _urllib_post(payload):
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except json.JSONDecodeError:
            detail = ""
        raise ModelError(f"OpenAI API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ModelError(f"OpenAI API unreachable: {e}") from e


def call_json(prompt, schema_name, schema, post=None):
    """One model call constrained to `schema`. Returns (parsed dict, call details for the run log)."""
    body = {"model": MODEL, "input": prompt,
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}}}
    resp = (post or _urllib_post)(body)
    texts = [c.get("text", "") for o in resp.get("output", []) if o.get("type") == "message"
             for c in o.get("content", [])]
    call = {"model": resp.get("model", MODEL), "status": resp.get("status"), "usage": resp.get("usage")}
    if not texts:
        raise ModelError(f"model returned no message (status {resp.get('status')})")
    text = texts[-1]
    try:
        return json.loads(text), call
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)       # tolerate prose or fences around the object
        if m:
            try:
                return json.loads(m.group(0)), call
            except json.JSONDecodeError:
                pass
        raise ModelError(f"model reply wasn't valid JSON: {text[:200]!r}")
