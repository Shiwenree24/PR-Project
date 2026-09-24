import base64
import os
import re
from collections import Counter
from datetime import datetime
from typing import Literal

import anthropic
from pydantic import BaseModel

from config import (
    USERNAME, AI_API_KEY, lookback_start, lookback_end, make_request,
    LOCK_FILES, REVERT_OVERLAP_THRESHOLD,
    MINOR_CHANGE_LINES, LINT_KEYWORDS, CONFIG_FILES,
    WIP_KEYWORDS, DUMMY_LINE_PATTERNS,
    TEST_FILE_PATTERNS, TEST_SHARE_THRESHOLD, MAJOR_DIFF_LINES,
    ADDRESS_KEYWORDS, COMMENT_LINE_PREFIXES,
    AI_MODEL, SURVIVAL_THRESHOLD,
    MEANINGFUL_COMMIT_WEIGHT, ADDRESS_COMMENT_WEIGHT, TESTING_WEIGHT,
    ADDITION_WEIGHT, DELETION_WEIGHT, LINES_CHANGED_WEIGHT,
    MEANINGFUL_PR_PERCENTILE, TOP_N_PRS
)
from reviews import get_pr_reviews

BUCKETS = ["lint/config", "WIP", "testing", "addressed comments", "meaningful"]
PREFILTER_REASONS = ["merge", "empty", "lock", "bot", "reverted"]


def parse_time(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def added_lines(f):
    return [l[1:] for l in f.get("patch", "").split("\n") if l.startswith("+")]


def removed_lines(f):
    return [l[1:] for l in f.get("patch", "").split("\n") if l.startswith("-")]


# ---------------------------------------------------------------------------
# fetch merged PRs — commits + diff, conversation/comments
# ---------------------------------------------------------------------------

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


def get_merge_commit_sha(repo_full_name, pr_number):
    """search results don't include the merge sha, so fetch the PR itself"""
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    return make_request(url).get("merge_commit_sha")


def get_pr_commits_with_diffs(repo_full_name, pr_number):
    """fetch all commits for a PR with their diffs (oldest first)"""
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


def get_final_file_lines(repo_full_name, file_path, ref):
    """
    fetch the final version of a file at the PR's merge commit as a set of stripped lines.
    returns an empty set if the file was deleted, None if the content can't be read (e.g. >1MB)
    """
    url = f"https://api.github.com/repos/{repo_full_name}/contents/{file_path}?ref={ref}"
    res = make_request(url)
    if not isinstance(res, dict):
        return None
    if res.get("message") == "Not Found":
        return set()
    if res.get("encoding") != "base64" or not res.get("content"):
        return None
    text = base64.b64decode(res["content"]).decode("utf-8", errors="replace")
    return {l.strip() for l in text.split("\n") if l.strip()}


# ---------------------------------------------------------------------------
# pre-filter — drop commits that shouldn't be analyzed at all
# ---------------------------------------------------------------------------

def is_bot_commit(commit):
    author = commit.get("author") or {}
    login = author.get("login", "")
    name = commit["commit"]["author"].get("name", "")
    return author.get("type") == "Bot" or login.endswith("[bot]") or name.endswith("[bot]")


def simple_filter_reason(commit_data):
    """simple: remove merge commits, empty, lock, bots"""
    commit = commit_data["commit"]
    files = commit_data["files"]
    stats = commit_data["stats"]

    if len(commit.get("parents", [])) > 1:
        return "merge"
    if not files or stats.get("additions", 0) + stats.get("deletions", 0) == 0:
        return "empty"
    if all(os.path.basename(f["filename"]) in LOCK_FILES for f in files):
        return "lock"
    if is_bot_commit(commit):
        return "bot"
    return None


def diff_signature(files):
    """multiset of (file, +/-, line) for every non-blank changed line"""
    sig = Counter()
    for f in files:
        for line in f.get("patch", "").split("\n"):
            if line[:1] in ("+", "-") and line[1:].strip():
                sig[(f["filename"], line[0], line[1:].strip())] += 1
    return sig


def invert_signature(sig):
    return Counter({(fn, "-" if sign == "+" else "+", text): n
                    for (fn, sign, text), n in sig.items()})


def signature_overlap(a, b):
    if not a or not b:
        return 0
    return sum((a & b).values()) / max(sum(a.values()), sum(b.values()))


def detect_revert_pairs(commits_with_diffs):
    """
    match commits whose diff is a near exact inverse (>= 90% overlap) of an earlier commit.
    both commits in a pair net to 0. a commit can only be in one pair, so a
    commit -> revert -> recommit chain collapses to the recommit being counted once.
    """
    signatures = [diff_signature(c["files"]) for c in commits_with_diffs]
    paired = set()
    for i in range(len(commits_with_diffs)):
        inverse = invert_signature(signatures[i])
        for j in range(i - 1, -1, -1):
            if j in paired:
                continue
            if signature_overlap(inverse, signatures[j]) >= REVERT_OVERLAP_THRESHOLD:
                paired.update((i, j))
                break
    return paired


def pre_filter(commits_with_diffs):
    """returns the commits worth analyzing plus counts of what was dropped and why"""
    dropped = {reason: 0 for reason in PREFILTER_REASONS}

    remaining = []
    for commit_data in commits_with_diffs:
        reason = simple_filter_reason(commit_data)
        if reason:
            dropped[reason] += 1
        else:
            remaining.append(commit_data)

    paired = detect_revert_pairs(remaining)
    dropped["reverted"] = len(paired)
    kept = [c for i, c in enumerate(remaining) if i not in paired]
    return kept, dropped


# ---------------------------------------------------------------------------
# meaningful commit organizer — runs per commit
# ---------------------------------------------------------------------------

class PRContext:
    """everything the organizer needs about the PR a commit belongs to"""

    def __init__(self, repo_full_name, pr_title, merge_sha, pr_comments, reviews):
        self.repo_full_name = repo_full_name
        self.pr_title = pr_title
        self.merge_sha = merge_sha
        self.pr_comments = pr_comments
        self.reviews = reviews
        self._final_files = {}

    def final_file_lines(self, path):
        if not self.merge_sha:
            return None
        if path not in self._final_files:
            self._final_files[path] = get_final_file_lines(
                self.repo_full_name, path, self.merge_sha
            )
        return self._final_files[path]


def normalize_for_lint(line):
    """strip formatting-only differences: whitespace, quote style, trailing ; and ,"""
    line = re.sub(r"\s+", "", line).replace("'", '"')
    return line.rstrip(";,")


def is_whitespace_only(files):
    """line diffs between commits that only move whitespace around"""
    for f in files:
        added = sorted(re.sub(r"\s+", "", l) for l in added_lines(f))
        removed = sorted(re.sub(r"\s+", "", l) for l in removed_lines(f))
        if added != removed:
            return False
    return True


def step1_lint_config(commit_data, message):
    """config-only files, simple lint, whitespace-only line diffs"""
    files = commit_data["files"]
    stats = commit_data["stats"]
    total_changes = stats.get("additions", 0) + stats.get("deletions", 0)
    changed_files = [f["filename"] for f in files]

    if all(any(cfg in f for cfg in CONFIG_FILES) for f in changed_files):
        return "lint/config"
    if total_changes <= MINOR_CHANGE_LINES:
        return "lint/config"
    if any(re.search(rf"\b{re.escape(kw)}\b", message) for kw in LINT_KEYWORDS):
        return "lint/config"
    if is_whitespace_only(files):
        return "lint/config"
    return None


def step2_wip_flags(commit_data, message):
    """
    flag (don't bucket) WIP/temp work — step 4 decides whether the flags hold.
    message flag: WIP keyword in the commit message
    content flag: every added line is a dummy/placeholder line
    """
    message_flag = any(re.search(rf"\b{re.escape(kw)}\b", message) for kw in WIP_KEYWORDS)

    added = [l for f in commit_data["files"] for l in added_lines(f) if l.strip()]
    content_flag = bool(added) and all(
        any(re.search(rf"\b{re.escape(p)}\b", l, re.IGNORECASE) for p in DUMMY_LINE_PATTERNS)
        for l in added
    )
    return message_flag, content_flag


def is_test_file(path):
    return any(pattern in path for pattern in TEST_FILE_PATTERNS)


def step3_test_check(commit_data):
    """test files make up most of the change and the non-test diff isn't a major change"""
    test_changes = 0
    other_changes = 0
    for f in commit_data["files"]:
        if is_test_file(f["filename"]):
            test_changes += f.get("changes", 0)
        else:
            other_changes += f.get("changes", 0)

    total = test_changes + other_changes
    if total == 0 or test_changes == 0:
        return None
    if test_changes / total >= TEST_SHARE_THRESHOLD and other_changes < MAJOR_DIFF_LINES:
        return "testing"
    return None


def step35_conversation_check(commit_data, message, ctx):
    """
    a commit addressed comments when a reviewer asked for changes before it
    and that same reviewer approved after it
    """
    commit = commit_data["commit"]
    commit_time = parse_time(commit["commit"]["committer"]["date"])
    commit_author = (commit.get("author") or {}).get("login")
    changed_files = {f["filename"] for f in commit_data["files"]}
    mentions_feedback = any(kw in message for kw in ADDRESS_KEYWORDS)

    approvals = {}
    for r in ctx.reviews:
        if r.get("state") == "APPROVED" and r.get("submitted_at"):
            approvals.setdefault(r["user"]["login"], []).append(parse_time(r["submitted_at"]))

    # (reviewer, time asked) for every request that plausibly led to this commit
    requests = []
    for r in ctx.reviews:
        if r.get("state") == "CHANGES_REQUESTED" and r.get("submitted_at"):
            requests.append((r["user"]["login"], parse_time(r["submitted_at"])))
    for c in ctx.pr_comments:
        on_changed_file = c.get("path") in changed_files
        if on_changed_file or mentions_feedback:
            requests.append((c.get("user", {}).get("login"), parse_time(c["created_at"])))

    for reviewer, asked_at in requests:
        if reviewer == commit_author or asked_at >= commit_time:
            continue
        if any(t >= commit_time for t in approvals.get(reviewer, [])):
            return "addressed comments"
    return None


def is_comment_line(line):
    return any(line.strip().startswith(p) for p in COMMENT_LINE_PREFIXES)


def is_lint_by_content(files):
    """every changed line is a comment, or the change is formatting only once normalized"""
    changed = [l for f in files for l in added_lines(f) + removed_lines(f) if l.strip()]
    if changed and all(is_comment_line(l) for l in changed):
        return True

    for f in files:
        added = Counter(normalize_for_lint(l) for l in added_lines(f) if l.strip())
        removed = Counter(normalize_for_lint(l) for l in removed_lines(f) if l.strip())
        if added != removed:
            return False
    return True


def survival_ratio(commit_data, ctx):
    """share of a commit's added lines that are still present in the final PR"""
    total = 0
    survived = 0
    for f in commit_data["files"]:
        final_lines = ctx.final_file_lines(f["filename"])
        if final_lines is None:
            continue
        for line in added_lines(f):
            if line.strip():
                total += 1
                survived += line.strip() in final_lines
    return survived / total if total else 1.0


def step4_lint_by_content(commit_data, wip_flags, ctx):
    """
    check lint by content rather than line diffs, then check whether the step 2 flags hold.
    any commit whose changes weren't driven into the final PR is WIP — checked here
    for every commit so overwritten work never reaches the AI
    """
    if is_lint_by_content(commit_data["files"]):
        return "lint/config"

    _, content_flag = wip_flags
    if content_flag:
        return "WIP"
    if survival_ratio(commit_data, ctx) < SURVIVAL_THRESHOLD:
        return "WIP"
    return None


class CommitAnalysis(BaseModel):
    category: Literal["meaningful", "lint/config", "WIP", "testing"]
    reason: str


AI_SYSTEM_PROMPT = """You review a single git commit from a merged pull request and judge how much it \
contributes to the PR. Classify it as:
- meaningful: real behavior changes, new features, design changes or creations, non-trivial fixes or refactors
- lint/config: formatting, renames with no behavior change, config or dependency bumps
- WIP: temporary, debugging, or throwaway work
- testing: changes that are primarily tests
Give a one-sentence reason."""

_ai_client = None


def analyze_with_ai(commit_data, ctx, wip_message_flag):
    """AI API: analyze the commit to see if it feeds into the importance of the PR"""
    global _ai_client
    if _ai_client is None:
        _ai_client = anthropic.Anthropic(api_key=AI_API_KEY) if AI_API_KEY else anthropic.Anthropic()

    commit = commit_data["commit"]
    diff = "\n\n".join(
        f"--- {f['filename']} ({f.get('status')})\n{f.get('patch', '[no patch — binary or too large]')}"
        for f in commit_data["files"]
    )
    note = "\nNote: the commit message suggests WIP, but its content looked substantive." if wip_message_flag else ""
    prompt = (
        f"PR title: {ctx.pr_title}\n"
        f"Commit message: {commit['commit']['message']}{note}\n\n"
        f"Diff:\n{diff}"
    )

    try:
        response = _ai_client.messages.parse(
            model=AI_MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            system=AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=CommitAnalysis,
        )
    except anthropic.APIError as e:
        print(f"  AI analysis failed for {commit['sha'][:7]} ({e}) — defaulting to meaningful")
        return "meaningful"

    if response.stop_reason == "refusal" or response.parsed_output is None:
        print(f"  AI analysis gave no answer for {commit['sha'][:7]} — defaulting to meaningful")
        return "meaningful"
    return response.parsed_output.category


def organize_commit(commit_data, ctx):
    """run a commit through steps 1 -> 4, then the AI, and return its bucket"""
    message = commit_data["commit"]["commit"]["message"].lower()

    bucket = step1_lint_config(commit_data, message)
    if bucket:
        return bucket

    wip_flags = step2_wip_flags(commit_data, message)

    bucket = step3_test_check(commit_data)
    if bucket:
        return bucket

    bucket = step35_conversation_check(commit_data, message, ctx)
    if bucket:
        return bucket

    bucket = step4_lint_by_content(commit_data, wip_flags, ctx)
    if bucket:
        return bucket

    return analyze_with_ai(commit_data, ctx, wip_flags[0])


# ---------------------------------------------------------------------------
# calculation + results
# ---------------------------------------------------------------------------

def calculate_pr_score(buckets, meaningful_additions, meaningful_deletions):
    """meaningful * 3 + addressed + testing, with additions weighed more than deletions"""
    weighted_lines = meaningful_additions * ADDITION_WEIGHT + meaningful_deletions * DELETION_WEIGHT
    score = (
        buckets["meaningful"] * MEANINGFUL_COMMIT_WEIGHT +
        buckets["addressed comments"] * ADDRESS_COMMENT_WEIGHT +
        buckets["testing"] * TESTING_WEIGHT +
        weighted_lines * LINES_CHANGED_WEIGHT
    )
    return round(score, 2)


def process_pr(repo_full_name, pr_number, pr_title):
    """process a single PR — pre-filter, organize every commit, and calculate score"""
    commits_with_diffs = get_pr_commits_with_diffs(repo_full_name, pr_number)
    ctx = PRContext(
        repo_full_name, pr_title,
        merge_sha=get_merge_commit_sha(repo_full_name, pr_number),
        pr_comments=get_pr_comments(repo_full_name, pr_number),
        reviews=get_pr_reviews(repo_full_name, pr_number),
    )

    kept, dropped = pre_filter(commits_with_diffs)

    buckets = {bucket: 0 for bucket in BUCKETS}
    meaningful_additions = 0
    meaningful_deletions = 0

    for commit_data in kept:
        bucket = organize_commit(commit_data, ctx)
        buckets[bucket] += 1
        if bucket == "meaningful":
            meaningful_additions += commit_data["stats"].get("additions", 0)
            meaningful_deletions += commit_data["stats"].get("deletions", 0)

    return {
        "pr_number": pr_number,
        "title": pr_title,
        "repo": repo_full_name,
        "buckets": buckets,
        "dropped": dropped,
        "meaningful_additions": meaningful_additions,
        "meaningful_deletions": meaningful_deletions,
        "score": calculate_pr_score(buckets, meaningful_additions, meaningful_deletions)
    }


def percentile_threshold(scores, percentile):
    """nearest-rank percentile of the scores"""
    if not scores:
        return 0
    ordered = sorted(scores)
    rank = max(1, round(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def percentile_rank(score, scores):
    return round(100 * sum(s <= score for s in scores) / len(scores))


# main
if __name__ == "__main__":
    merged_prs = get_merged_prs(USERNAME)

    total_buckets = {bucket: 0 for bucket in BUCKETS}
    total_dropped = {reason: 0 for reason in PREFILTER_REASONS}
    pr_results = []

    print(f"Processing {len(merged_prs)} merged PRs...")

    for pr in merged_prs:
        repo_full_name = pr["repository_url"].split("repos/")[1]
        result = process_pr(repo_full_name, pr["number"], pr["title"])
        pr_results.append(result)

        for key, val in result["buckets"].items():
            total_buckets[key] += val
        for key, val in result["dropped"].items():
            total_dropped[key] += val

    pr_results.sort(key=lambda x: x["score"], reverse=True)
    scores = [pr["score"] for pr in pr_results]
    threshold = percentile_threshold(scores, MEANINGFUL_PR_PERCENTILE)
    meaningful_prs = [pr for pr in pr_results if pr["score"] > 0 and pr["score"] >= threshold]

    print(f"\nGitHub PR stats for {USERNAME} ({lookback_start.strftime('%Y-%m-%d')} to {lookback_end.strftime('%Y-%m-%d')}):")
    print(f"  PRs merged: {len(merged_prs)}")
    print(f"  Meaningful PRs (score >= {threshold}, {MEANINGFUL_PR_PERCENTILE}th percentile): {len(meaningful_prs)}")

    print(f"\nCommits dropped by pre-filter:")
    for reason, count in total_dropped.items():
        print(f"  {reason}: {count}")

    print(f"\nCommit buckets across merged PRs:")
    for bucket, count in total_buckets.items():
        print(f"  {bucket}: {count}")

    print(f"\nPR ranking by effort (top {TOP_N_PRS}):")
    for i, pr in enumerate(pr_results[:TOP_N_PRS], 1):
        print(f"\n  {i}. PR #{pr['pr_number']} - {pr['title']}")
        print(f"     Repo: {pr['repo']}")
        print(f"     Score: {pr['score']} ({percentile_rank(pr['score'], scores)}th percentile)")
        print(f"     Meaningful commits: {pr['buckets']['meaningful']}")
        print(f"     Addressed comment commits: {pr['buckets']['addressed comments']}")
        print(f"     Testing commits: {pr['buckets']['testing']}")
        print(f"     Meaningful lines: +{pr['meaningful_additions']} / -{pr['meaningful_deletions']}")
