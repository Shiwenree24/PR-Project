from datetime import datetime, timedelta

GITHUB_TOKEN = "your_token_here"
USERNAME = "target_username_here"
AI_API_KEY = "your_ai_api_key_here"

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

six_months_ago = datetime.now() - timedelta(days=180)

# commit message keywords
WIP_KEYWORDS = ["wip", "temp", "draft", "checkpoint", "tmp", "fixup", "squash"]
ADDRESS_KEYWORDS = ["address", "fix review", "pr feedback", "address comments",
                    "address feedback", "review comments", "pr comments"]
LINT_KEYWORDS = ["lint", "format", "whitespace", "style", "prettier", "eslint"]
TEST_KEYWORDS = ["test", "spec", "unittest", "pytest"]
DUMMY_TEST_KEYWORDS = ["dummy test", "placeholder test", "temp test", "wip test"]
CONFIG_FILES = [".eslintrc", ".prettierrc", ".babelrc", "jest.config",
                "tsconfig", ".gitignore", "package.json", "requirements.txt"]

# diff line keywords
TEST_LINE_PATTERNS = ["def test_", "describe(", "it(", "assert", "expect(", 
                      "test(", "beforeEach", "afterEach"]
DUMMY_LINE_PATTERNS = ["pass", "TODO", "placeholder", "temp", "fixme", "hack"]
LINT_LINE_PATTERNS = ["# ", "// ", "/* "]

# scoring weights
MEANINGFUL_COMMIT_WEIGHT = 3
ADDRESS_COMMENT_WEIGHT = 1
LINES_CHANGED_WEIGHT = 0.01