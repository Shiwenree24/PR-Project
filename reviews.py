import requests
from datetime import datetime
from config import (
    GITHUB_TOKEN, USERNAME, headers, six_months_ago
)

def get_pr_reviews(repo_full_name, pr_number):
    """fetch all reviews for a PR"""
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews?per_page=100"
    return requests.get(url, headers=headers).json()

def get_pr_comments(repo_full_name, pr_number):
    """fetch all inline and general PR comments"""
    comments = []
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/comments?per_page=100"
    res = requests.get(url, headers=headers).json()
    comments.extend(res)
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments?per_page=100"
    res = requests.get(url, headers=headers).json()
    comments.extend(res)
    return comments

def get_pr_commits(repo_full_name, pr_number):
    """fetch all commits for a PR — used for cross referencing comment timestamps"""
    commits = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/commits?per_page=100&page={page}"
        res = requests.get(url, headers=headers).json()
        commits.extend(res)
        if len(res) < 100:
            break
        page += 1
    return commits

def analyze_review_impact(username, reviews, pr_comments, commits):
    """
    analyze whether reviews given by username were actioned upon
    signal 1: REQUEST_CHANGES followed by APPROVED from same reviewer
    signal 2: PR author replied to or a commit was made after reviewer comment
    """
    actioned_count = 0
    acknowledged_count = 0
    no_response_count = 0

    # get all reviews by this user
    user_reviews = [r for r in reviews if r["user"]["login"] == username]

    # get commit times for cross referencing
    commit_times = [
        datetime.strptime(
            c["commit"]["committer"]["date"], "%Y-%m-%dT%H:%M:%SZ"
        )
        for c in commits
    ]

    # signal 1: REQUEST_CHANGES followed by APPROVED from same reviewer
    request_changes_reviews = [r for r in user_reviews if r["state"] == "CHANGES_REQUESTED"]
    approved_reviews = [r for r in user_reviews if r["state"] == "APPROVED"]

    for rc_review in request_changes_reviews:
        rc_time = datetime.strptime(rc_review["submitted_at"], "%Y-%m-%dT%H:%M:%SZ")
        later_approvals = [
            r for r in approved_reviews
            if datetime.strptime(r["submitted_at"], "%Y-%m-%dT%H:%M:%SZ") > rc_time
        ]
        if later_approvals:
            actioned_count += 1

    # signal 2: reply or commit after reviewer inline comment
    reviewer_comments = [
        c for c in pr_comments
        if c.get("user", {}).get("login") == username
        and "in_reply_to_id" not in c  # only top level comments not replies
    ]

    for comment in reviewer_comments:
        comment_id = comment["id"]
        comment_time = datetime.strptime(comment["created_at"], "%Y-%m-%dT%H:%M:%SZ")

        # check if someone else replied to this comment
        replies = [
            c for c in pr_comments
            if c.get("in_reply_to_id") == comment_id
            and c.get("user", {}).get("login") != username
        ]

        # check if a commit was made after this comment
        commits_after = [t for t in commit_times if t > comment_time]

        if replies or commits_after:
            acknowledged_count += 1
        else:
            no_response_count += 1

    return {
        "actioned": actioned_count,
        "acknowledged": acknowledged_count,
        "no_response": no_response_count
    }

def get_reviews_given(username):
    """get all PRs reviewed by username and analyze review impact"""
    review_count = 0
    approval_count = 0
    total_actioned = 0
    total_acknowledged = 0
    total_no_response = 0

    page = 1
    while True:
        url = f"https://api.github.com/search/issues?q=type:pr+reviewed-by:{username}+created:>{six_months_ago.strftime('%Y-%m-%d')}&per_page=100&page={page}"
        res = requests.get(url, headers=headers).json()
        items = res.get("items", [])

        for pr in items:
            repo_full_name = pr["repository_url"].split("repos/")[1]
            pr_number = pr["number"]

            # fetch everything needed for this PR
            reviews = get_pr_reviews(repo_full_name, pr_number)
            pr_comments = get_pr_comments(repo_full_name, pr_number)
            commits = get_pr_commits(repo_full_name, pr_number)

            # count reviews and approvals
            for review in reviews:
                if review["user"]["login"] == username:
                    review_count += 1
                    if review["state"] == "APPROVED":
                        approval_count += 1

            # analyze review impact
            impact = analyze_review_impact(username, reviews, pr_comments, commits)
            total_actioned += impact["actioned"]
            total_acknowledged += impact["acknowledged"]
            total_no_response += impact["no_response"]

        if len(items) < 100:
            break
        page += 1

    return {
        "review_count": review_count,
        "approval_count": approval_count,
        "actioned": total_actioned,
        "acknowledged": total_acknowledged,
        "no_response": total_no_response
    }

# main
if __name__ == "__main__":
    print(f"Fetching review stats for {USERNAME}...")
    review_stats = get_reviews_given(USERNAME)

    print(f"\nGitHub review stats for {USERNAME} (last 6 months):")
    print(f"  Total reviews given: {review_stats['review_count']}")
    print(f"  Approvals given: {review_stats['approval_count']}")
    print(f"  Reviews actioned (REQUEST_CHANGES -> APPROVED): {review_stats['actioned']}")
    print(f"  Reviews acknowledged (reply or commit after): {review_stats['acknowledged']}")
    print(f"  Reviews with no response: {review_stats['no_response']}")