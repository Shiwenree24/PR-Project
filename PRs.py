from datetime import datetime
from config import (
    USERNAME, AI_API_KEY, lookback_start, lookback_end, make_request,
    WIP_KEYWORDS, ADDRESS_KEYWORDS, LINT_KEYWORDS, TEST_FILE_PATTERNS,
    CONFIG_FILES, DUMMY_LINE_PATTERNS, LINT_LINE_PATTERNS,
    MEANINGFUL_COMMIT_WEIGHT, ADDRESS_COMMENT_WEIGHT, LINES_CHANGED_WEIGHT
)

def get_merged_prs(username):
    """fetch all merged PRs by username in lookback window"""
    prs = []
    page = 1
    while True:
        url = f"https://api.github.com/search/issues?q=type:pr+author:{username}+is:merged+merged:{lookback_start.strftime('%Y-%m-%d')}..{lookback_end.strftime('%Y-%m-%d')}&per_page=100&page={page}"
        res = make_request(url)
        items = res.get("items", [])
        prs.extend(items)
        if len(items) < 100:
            break
        page += 1
    return prs

def get_pr_commits_with_diffs(repo_full_name, pr_number):
    """fetch all commits for a PR with their diffs"""
    commits = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/commits?per_page=100&page={page}"
        res = make_request(url)
        commits.extend(res)
        if len(res) < 100:
            break
        page += 1

    commits_with_diffs = []
    for commit in commits:
        sha = commit["sha"]
        url = f"https://api.github.com/repos/{repo_full_name}/commits/{sha}"
        detail = make_request(url)
        commits_with_diffs.append({
            "commit": commit,
            "files": detail.get("files", []),
            "stats": detail.get("stats", {})
        })
    return commits_with_diffs

def get_pr_comments(repo_full_name, pr_number):
    """fetch all inline and general PR comments"""
    comments = []
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/comments?per_page=100"
    comments.extend(make_request(url))
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments?per_page=100"
    comments.extend(make_request(url))
    return comments

def get_final_file_version(repo_full_name, file_path, ref):
    """fetch final version of a file at a given ref — only called for AI analysis"""
    url = f"https://api.github.com/repos/{repo_full_name}/contents/{file_path}?ref={ref}"
    res = make_request(url)
    return res.get("content", "")

def scan_diff_lines(files):
    """stage 3.5 — scan actual diff patch lines for keywords before AI"""
    for f in files:
        patch = f.get("patch", "")
        if not patch:
            continue
        added_lines = [l for l in patch.split("\n") if l.startswith("+")]

        # check for dummy/placeholder patterns
        if added_lines and all(any(pattern in l for pattern in DUMMY_LINE_PATTERNS) 
                               for l in added_lines if l.strip()):
            return "WIP/temp"

        # check for lint only changes
        if added_lines and all(l.strip() == "" or any(pattern in l for pattern in LINT_LINE_PATTERNS)
               for l in added_lines):
            return "lint/minor"

    return None

def is_test_commit(files):
    """check if all changed files are test files based on file paths"""
    changed_files = [f["filename"] for f in files]
    if not changed_files:
        return False
    return all(any(pattern in f for pattern in TEST_FILE_PATTERNS) for f in changed_files)

def classify_commit(commit_data, pr_comments):
    """run commit through classification pipeline"""
    commit = commit_data["commit"]
    files = commit_data["files"]
    stats = commit_data["stats"]
    message = commit["commit"]["message"].lower()
    commit_time = datetime.strptime(
        commit["commit"]["committer"]["date"], "%Y-%m-%dT%H:%M:%SZ"
    )

    total_changes = stats.get("additions", 0) + stats.get("deletions", 0)
    changed_files = [f["filename"] for f in files]
    is_config_only = all(any(cfg in f for cfg in CONFIG_FILES) for f in changed_files)

    # stage 1 — size and lint filter
    if total_changes <= 2 or is_config_only:
        return "lint/minor", 0
    if any(kw in message for kw in LINT_KEYWORDS):
        return "lint/minor", 0

    # stage 2 — keyword and file based filter
    if any(message.startswith(kw) or f" {kw} " in message or message == kw
           for kw in WIP_KEYWORDS):
        return "WIP/temp", 0

    if is_test_commit(files):
        return "testing", 0

    # stage 3 — PR conversation correlation
    if any(kw in message for kw in ADDRESS_KEYWORDS):
        recent_comments = [
            c for c in pr_comments
            if datetime.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%SZ") < commit_time
        ]
        if recent_comments:
            return "address comments", total_changes

    # stage 3.5 — scan diff lines
    diff_classification = scan_diff_lines(files)
    if diff_classification:
        return diff_classification, 0

    # stage 4 — needs AI analysis
    return "needs_ai_analysis", total_changes

def analyze_with_ai(commit_data, final_file_contents):
    """placeholder for Claude API call"""
    # TODO: implement Claude API call using AI_API_KEY
    return "meaningful"

def calculate_pr_score(buckets, meaningful_lines):
    """score a PR based on meaningful commits, address comments, and lines changed"""
    score = (
        buckets["meaningful"] * MEANINGFUL_COMMIT_WEIGHT +
        buckets["address comments"] * ADDRESS_COMMENT_WEIGHT +
        meaningful_lines * LINES_CHANGED_WEIGHT
    )
    return round(score, 2)

def process_pr(repo_full_name, pr_number, pr_merge_sha, pr_title):
    """process a single PR — classify commits and calculate score"""
    commits_with_diffs = get_pr_commits_with_diffs(repo_full_name, pr_number)
    pr_comments = get_pr_comments(repo_full_name, pr_number)

    buckets = {
        "meaningful": 0,
        "address comments": 0,
        "testing": 0,
        "WIP/temp": 0,
        "lint/minor": 0,
        "excluded": 0,
        "needs_ai_analysis": 0
    }
    meaningful_lines = 0

    for commit_data in commits_with_diffs:
        classification, lines = classify_commit(commit_data, pr_comments)

        if classification == "needs_ai_analysis":
            final_files = {}
            if pr_merge_sha:
                for f in commit_data["files"]:
                    final_files[f["filename"]] = get_final_file_version(
                        repo_full_name, f["filename"], pr_merge_sha
                    )
            classification = analyze_with_ai(commit_data, final_files)
            if classification == "meaningful":
                meaningful_lines += lines

        if classification == "meaningful":
            meaningful_lines += lines

        buckets[classification] = buckets.get(classification, 0) + 1

    score = calculate_pr_score(buckets, meaningful_lines)

    return {
        "pr_number": pr_number,
        "title": pr_title,
        "repo": repo_full_name,
        "buckets": buckets,
        "meaningful_lines": meaningful_lines,
        "score": score
    }

# main
if __name__ == "__main__":
    merged_prs = get_merged_prs(USERNAME)

    total_buckets = {
        "meaningful": 0,
        "address comments": 0,
        "testing": 0,
        "WIP/temp": 0,
        "lint/minor": 0,
        "excluded": 0,
        "needs_ai_analysis": 0
    }

    pr_results = []

    print(f"Processing {len(merged_prs)} merged PRs...")

    for pr in merged_prs:
        repo_full_name = pr["repository_url"].split("repos/")[1]
        pr_number = pr["number"]
        pr_title = pr["title"]
        pr_merge_sha = pr.get("pull_request", {}).get("merged_at", None)

        result = process_pr(repo_full_name, pr_number, pr_merge_sha, pr_title)
        pr_results.append(result)

        for key, val in result["buckets"].items():
            total_buckets[key] = total_buckets.get(key, 0) + val

    pr_results.sort(key=lambda x: x["score"], reverse=True)

    print(f"\nGitHub PR stats for {USERNAME} ({lookback_start.strftime('%Y-%m-%d')} to {lookback_end.strftime('%Y-%m-%d')}):")
    print(f"  PRs merged: {len(merged_prs)}")

    print(f"\nCommit breakdown across merged PRs:")
    for bucket, count in total_buckets.items():
        print(f"  {bucket}: {count}")

    print(f"\nPR ranking by effort (top 10):")
    for i, pr in enumerate(pr_results[:10], 1):
        print(f"\n  {i}. PR #{pr['pr_number']} - {pr['title']}")
        print(f"     Repo: {pr['repo']}")
        print(f"     Score: {pr['score']}")
        print(f"     Meaningful commits: {pr['buckets']['meaningful']}")
        print(f"     Address comment commits: {pr['buckets']['address comments']}")
        print(f"     Lines changed in meaningful commits: {pr['meaningful_lines']}")