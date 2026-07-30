from datetime import datetime, timedelta
import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
USERNAME = os.getenv("USERNAME")
AI_API_KEY = os.getenv("AI_API_KEY")

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# time window — set START_DATE and END_DATE in .env for your review cycle
# format: YYYY-MM-DD
START_DATE = os.getenv("START_DATE")
END_DATE = os.getenv("END_DATE")

if START_DATE and END_DATE:
    lookback_start = datetime.strptime(START_DATE, "%Y-%m-%d")
    lookback_end = datetime.strptime(END_DATE, "%Y-%m-%d")
elif START_DATE:
    lookback_start = datetime.strptime(START_DATE, "%Y-%m-%d")
    lookback_end = datetime.now()
else:
    # fallback to LOOKBACK_DAYS if no dates provided
    lookback_start = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    lookback_end = datetime.now()

# commit message keywords — using stricter matching
WIP_KEYWORDS = ["wip", "tmp", "fixup", "squash", "checkpoint"]
ADDRESS_KEYWORDS = ["address comments", "fix review", "pr feedback",
                    "address feedback", "review comments", "pr comments"]
LINT_KEYWORDS = ["lint", "prettier", "eslint", "whitespace"]

# test file patterns — file based detection instead of message keywords
TEST_FILE_PATTERNS = [
    "/test/", "/tests/", "/__tests__/", "/spec/",
    ".test.js", ".test.ts", ".spec.js", ".spec.ts",
    "_test.py", "test_", "Test.java", "Spec.java"
]

CONFIG_FILES = [".eslintrc", ".prettierrc", ".babelrc", "jest.config",
                "tsconfig", ".gitignore", "package.json", "requirements.txt"]

# diff line keywords
DUMMY_LINE_PATTERNS = ["pass", "TODO", "placeholder", "fixme", "hack"]
LINT_LINE_PATTERNS = ["# ", "// ", "/* "]

# scoring weights
MEANINGFUL_COMMIT_WEIGHT = 3
ADDRESS_COMMENT_WEIGHT = 1
LINES_CHANGED_WEIGHT = 0.01

# rate limiting
RATE_LIMIT_PAUSE = 1  # seconds to wait between API calls
RATE_LIMIT_THRESHOLD = 100  # pause more aggressively when remaining calls drop below this

def make_request(url):
    """wrapper for all API calls with rate limit handling"""
    while True:
        response = requests.get(url, headers=headers)
        
        # check rate limit headers
        remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
        reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
        
        if response.status_code == 403 and remaining == 0:
            # fully rate limited — wait until reset
            wait_time = reset_time - int(time.time()) + 5  # +5 buffer
            print(f"Rate limit hit — waiting {wait_time} seconds...")
            time.sleep(max(wait_time, 0))
            continue  # retry
        
        if remaining < RATE_LIMIT_THRESHOLD:
            # getting close — slow down
            print(f"Approaching rate limit ({remaining} remaining) — slowing down...")
            time.sleep(5)
        else:
            # normal pace
            time.sleep(RATE_LIMIT_PAUSE)
        
        return response.json()