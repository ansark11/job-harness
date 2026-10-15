"""OpenAI Responses API calls with structured (JSON schema) output.

IDENTICAL COPIES live in every service that calls the model; the master is
scripts/llm.py (scripts/test.sh fails if a copy drifts). Same pattern as finance-harness card-offers/discover.py: stdlib urllib, key
from the environment or this service's .env, and an injectable `post` so
tests run on canned replies and never reach the network.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "gpt-6-luna"
API_URL = "https://api.openai.com/v1/responses"
ENV_PATH = Path(__file__).resolve().parent / ".env"
RETRY_WAITS = (5, 20)                 # seconds before each retry of a transient failure
RETRY_HTTP = {408, 429, 500, 502, 503, 504}


class ModelError(Exception):
    pass


class TransientError(ModelError):
    """A failure worth retrying: timeout, dropped connection, rate limit, server error."""


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
        except (json.JSONDecodeError, OSError):
            detail = ""
        err = TransientError if e.code in RETRY_HTTP else ModelError
        raise err(f"OpenAI API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise TransientError(f"OpenAI API unreachable: {e.reason}") from e
    except (TimeoutError, ConnectionError, OSError) as e:      # read timeouts, resets mid-response
        raise TransientError(f"OpenAI API connection failed: {type(e).__name__}: {e}") from e


sleep = time.sleep      # tests replace this

# Contact details never go to the model (the user's rule). Callers already send text with
# them removed; this is the safety net for anything shaped like an email, phone or profile URL.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)(\+?1[\s.-]?)?(\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\w)")
_LINKEDIN = re.compile(r"(https?://)?([\w-]+\.)?linkedin\.com/in/[\w%-]+/?", re.I)


def scrub_contact(text):
    return _PHONE.sub("[phone]", _LINKEDIN.sub("[linkedin]", _EMAIL.sub("[email]", text)))


def call_json(prompt, schema_name, schema, post=None):
    """One model call constrained to `schema`. Returns (parsed dict, call details for the run log)."""
    body = {"model": MODEL, "input": scrub_contact(prompt),
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}}}
    send = post or _urllib_post
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            resp = send(body)
            break
        except TransientError:
            if attempt == len(RETRY_WAITS):
                raise
            sleep(RETRY_WAITS[attempt])
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
