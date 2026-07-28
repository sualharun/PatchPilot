from __future__ import annotations

import re
import time
from typing import Any

import jwt
import requests

from agent.domain.entities import IssueContext
from agent.domain.value_objects import IssueRef

PR_LINK_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")


class GitHubClient:
    def __init__(self, token: str | None = None, api_base: str = "https://api.github.com") -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def fetch_issue(self, ref: IssueRef) -> IssueContext:
        issue = self._get(f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}")
        comments_raw = self._get(issue.get("comments_url", ""))
        comments = [comment.get("body") or "" for comment in comments_raw]
        body = issue.get("body") or ""
        labels = [label.get("name", "") for label in issue.get("labels", []) if label.get("name")]
        linked_prs = sorted(set(PR_LINK_RE.findall(body + "\n" + "\n".join(comments))))
        linked_prs.extend(self._timeline_pr_links(ref))
        return IssueContext(
            ref=ref,
            title=issue.get("title") or "",
            body=body,
            comments=tuple(comments),
            labels=tuple(labels),
            linked_prs=tuple(sorted(set(linked_prs))),
        )

    def clone_url(self, ref: IssueRef) -> str:
        if self.token:
            return f"https://x-access-token:{self.token}@github.com/{ref.owner}/{ref.repo}.git"
        return f"https://github.com/{ref.owner}/{ref.repo}.git"

    def create_draft_pr(self, ref: IssueRef, branch: str, title: str, body: str) -> str:
        repo = self._get(f"/repos/{ref.owner}/{ref.repo}")
        default_branch = repo.get("default_branch", "main")
        payload = {
            "title": title,
            "head": branch,
            "base": default_branch,
            "body": body,
            "draft": True,
            "maintainer_can_modify": True,
        }
        pr = self._post(f"/repos/{ref.owner}/{ref.repo}/pulls", payload)
        return pr["html_url"]

    def fetch_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}")

    def fetch_pull_request_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._get(f"/repos/{owner}/{repo}/pulls/{number}/files")

    def post_pull_request_comment(self, owner: str, repo: str, number: int, body: str) -> str:
        comment = self._post(f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})
        return str(comment.get("html_url", ""))

    def create_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        state: str,
        context: str,
        description: str,
        target_url: str | None = None,
    ) -> str:
        payload = {
            "state": state,
            "context": context,
            "description": description[:140],
        }
        if target_url:
            payload["target_url"] = target_url
        status = self._post(f"/repos/{owner}/{repo}/statuses/{sha}", payload)
        return str(status.get("url", ""))

    def current_user(self) -> dict[str, Any]:
        return self._get("/user")

    def list_accessible_repositories(self, limit: int = 100) -> list[dict[str, Any]]:
        repos = self._get(f"/user/repos?per_page={min(limit, 100)}&sort=updated")
        return [
            {
                "full_name": repo.get("full_name"),
                "private": repo.get("private"),
                "default_branch": repo.get("default_branch"),
                "permissions": repo.get("permissions") or {},
            }
            for repo in repos
        ]

    def verify_repository_access(self, full_name: str) -> dict[str, Any]:
        owner, repo = full_name.split("/", 1)
        payload = self._get(f"/repos/{owner}/{repo}")
        permissions = payload.get("permissions") or {}
        return {
            "full_name": payload.get("full_name", full_name),
            "default_branch": payload.get("default_branch", "main"),
            "private": bool(payload.get("private")),
            "can_pull": bool(permissions.get("pull", True)),
            "can_push": bool(permissions.get("push", False)),
        }

    @classmethod
    def from_github_app_installation(
        cls,
        *,
        app_id: str,
        private_key: str,
        installation_id: str,
        api_base: str = "https://api.github.com",
    ) -> GitHubClient:
        app_jwt = create_github_app_jwt(app_id=app_id, private_key=private_key)
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {app_jwt}",
            }
        )
        response = session.post(
            f"{api_base.rstrip('/')}/app/installations/{installation_id}/access_tokens",
            timeout=30,
        )
        response.raise_for_status()
        token = response.json()["token"]
        return cls(token=token, api_base=api_base)

    def _timeline_pr_links(self, ref: IssueRef) -> list[str]:
        try:
            events = self._get(f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/timeline")
        except requests.HTTPError:
            return []
        links: list[str] = []
        for event in events:
            source = event.get("source") or {}
            issue = source.get("issue") or {}
            html_url = issue.get("html_url")
            if html_url and "/pull/" in html_url:
                links.append(html_url)
        return links

    def _get(self, path_or_url: str) -> Any:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_base}{path_or_url}"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        response = self.session.post(f"{self.api_base}{path}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()


def create_github_app_jwt(*, app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 9 * 60,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")
