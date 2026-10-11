# Test runs must never write into the real logs/ used for debugging:
# send every component's structured log (runlog.py) to a throwaway dir.
import os
import tempfile

os.environ["HARNESS_LOG_DIR"] = tempfile.mkdtemp(prefix="harness-test-logs-")
