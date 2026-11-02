"""Shared GitHub transport and repository operations for GitHub automations."""

import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from functools import cached_property
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen


# The secret name of the GitHub token an OpenHands Cloud or Enterprise user's
# connected GitHub integration provides: how this run's sandbox serves it, and
# how a conversation sees it in its environment.
CLOUD_GITHUB_TOKEN_SECRET = "github_token"
CLOUD_GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

DEFAULT_GITHUB_API = "https://api.github.com"
GITHUB_HOSTS = {"github.com", "www.github.com"}
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OPENHANDS_MENTION_RE = re.compile(r"(?i)(?<![\w.])@openhands\b")
BOT_REPLY_MARKERS = ("AI agent (OpenHands)", "openhands-reply")


def _item_time(item: dict) -> float:
    value = item.get("submitted_at") or item.get("created_at") or ""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def normalize_repository(value: str) -> str:
    """Return ``owner/repo`` for any of the ways a repository is written down.

    The setup form on a local deployment is a plain text box, so what users
    paste is usually the clone URL their repository page offers. Passing that
    through to the API builds ``/repos/https://host/owner/repo.git``, which the
    forge answers with a 404 - indistinguishable from a repository the token
    cannot see.
    """
    repo = (value or "").strip().rstrip("/")
    if repo.startswith("git@"):
        # git@host:owner/repo.git
        repo = repo.partition(":")[2]
    elif "://" in repo:
        # https://host/owner/repo.git (and any other host)
        repo = urlsplit(repo).path.lstrip("/")
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    repo = repo.strip("/")
    if not REPO_NAME_RE.match(repo):
        raise ValueError(
            f"{value!r} is not a repository. Use owner/repo or a clone URL for "
            "one repository, for example OpenHands/automation or "
            "https://github.com/OpenHands/automation.git."
        )
    return repo


def repository_host(value: str) -> str | None:
    """The host a repository clone URL names, or None for an ``owner/repo``."""
    raw = (value or "").strip()
    if "://" in raw:
        return urlsplit(raw).netloc
    if raw.startswith("git@"):
        # git@host:owner/repo.git
        return raw[4:].partition(":")[0].split("/", 1)[0] or None
    return None


def detect_forge(host: str | None, configured: str | None = None) -> str:
    """Which API dialect the repository host speaks.

    GitHub's API is the default for github.com and for a bare ``owner/repo``.
    Every other host is treated as Gitea, whose API is the closest fit; a
    deployment that names a different forge in config wins over the guess.
    """
    if configured:
        return configured.strip().lower()
    if not host or host.lower() in GITHUB_HOSTS:
        return "github"
    return "gitea"


def is_cloud_run() -> bool:
    """Whether this run is on OpenHands Cloud or Enterprise.

    The automation service hands a run the Agent Server URL only on a local
    Agent Canvas; elsewhere the run talks to the OpenHands API instead.
    """
    return bool(os.environ.get("OPENHANDS_CLOUD_API_URL")) and not os.environ.get(
        "AGENT_SERVER_URL"
    )


def _load_cloud_secret(name: str) -> str | None:
    """Read one named secret of this run's sandbox from the OpenHands API."""
    api = os.environ["OPENHANDS_CLOUD_API_URL"].rstrip("/")
    request = Request(
        f"{api}/api/v1/sandboxes/{os.environ['SANDBOX_ID']}/settings/secrets/{name}",
        headers={"X-Session-API-Key": os.environ["SESSION_API_KEY"]},
    )
    try:
        with urlopen(request, timeout=90) as response:
            return response.read().decode().strip()
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _load_secret(name: str) -> str:
    """Read one named secret from the environment, or from the configured Agent
    Server on a local run and the OpenHands API on a cloud one."""
    value = os.environ.get(name)
    if value:
        return value

    if is_cloud_run():
        value = _load_cloud_secret(name)
    else:
        from openhands.sdk.workspace import RemoteWorkspace

        workspace = RemoteWorkspace(
            host=os.environ["AGENT_SERVER_URL"],
            api_key=os.environ["SESSION_API_KEY"],
            working_dir=os.environ.get("WORKSPACE_BASE", "/workspace"),
        )
        try:
            secret = workspace.get_secrets([name]).get(name)
            value = secret.get_value() if secret else None
        finally:
            workspace.reset_client()
    if not value:
        raise ValueError(f"The GitHub credential {name} is unavailable")
    return value


def github_request(
    token: str,
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
    accept: str = "application/vnd.github+json",
    *,
    api_base: str = DEFAULT_GITHUB_API,
    token_style: str = "bearer",
) -> tuple:
    url = f"{api_base.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {
        "Authorization": (
            f"token {token}" if token_style == "token" else f"Bearer {token}"
        ),
        "Accept": accept,
        "Content-Type": "application/json",
    }
    if token_style != "token":
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=90) as r:
        raw = r.read()
        return (json.loads(raw) if raw.strip() else {}), dict(r.headers)


def github_paginate(
    token: str,
    path: str,
    params: dict | None = None,
    *,
    api_base: str = DEFAULT_GITHUB_API,
    token_style: str = "bearer",
    page_size_param: str = "per_page",
    page_size: int = 100,
) -> list:
    results = []
    base_params = dict(params or {})
    base_params.setdefault(page_size_param, page_size)
    for page in range(1, 101):
        base_params["page"] = page
        data, _ = github_request(
            token,
            "GET",
            path,
            params=base_params,
            api_base=api_base,
            token_style=token_style,
        )
        if not isinstance(data, list):
            raise TypeError("Expected a paginated repository list")
        results.extend(data)
        if len(data) < int(base_params[page_size_param]):
            return results
    raise RuntimeError("Repository pagination exceeded limit")


class GitHubRepository:
    name = "GitHub automation"

    def __init__(
        self,
        config_path=Path("config.json"),
        *,
        github_token_secret,
        repository=None,
        conversation=None,
        dispatcher=None,
    ):
        self.config = json.loads(Path(config_path).read_text())
        raw_repository = repository or self.config["repository"]
        self.repository = normalize_repository(raw_repository)
        host = repository_host(raw_repository) or self.config.get("host")
        self.forge = detect_forge(host, self.config.get("forge"))
        self.host = host or ("github.com" if self.forge == "github" else "")
        if not self.host:
            raise ValueError(
                "A non-GitHub repository needs a host. Pass a clone URL in "
                "repos, or set 'host' in config.json."
            )
        self.clone_url = (
            self.config.get("clone_url")
            or (raw_repository if "://" in raw_repository else "")
            or (
                f"https://github.com/{self.repository}.git"
                if self.forge == "github"
                else f"https://{self.host}/{self.repository}.git"
            )
        )
        self.web_url = self.config.get("web_url") or (
            f"https://github.com/{self.repository}"
            if self.forge == "github"
            else f"https://{self.host}/{self.repository}"
        )
        self.api_base = (
            self.config.get("api_url")
            or (
                DEFAULT_GITHUB_API
                if self.forge == "github"
                else f"https://{self.host}/api/v1"
            )
        ).rstrip("/")
        self.token_style = "token" if self.forge == "gitea" else "bearer"
        self.accept = (
            "application/json"
            if self.forge == "gitea"
            else "application/vnd.github+json"
        )

        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", github_token_secret):
            self.token_name = github_token_secret
            try:
                self.token = _load_secret(github_token_secret)
            except ValueError:
                if not is_cloud_run():
                    raise
                # No secret is saved under that name, so use the user's connected
                # GitHub integration, under the name a conversation sees it by.
                self.token = _load_cloud_secret(CLOUD_GITHUB_TOKEN_SECRET)
                if not self.token:
                    raise
                self.token_name = CLOUD_GITHUB_TOKEN_ENV
        else:
            # The setup form asks for a secret *name*, but on a local deployment
            # it is a free text box and the token itself is easy to paste into
            # it. Accept it rather than making the run fail before it starts;
            # the name below is the environment variable the conversation sees.
            self.token_name = (
                "GITHUB_TOKEN" if self.forge == "github" else "GITEA_TOKEN"
            )
            self.token = github_token_secret
            if not self.token:
                raise ValueError("GitHub token secret is empty")
        self.conversation = conversation
        self.conversation_id = str(conversation.id) if conversation else None
        self.dispatcher = dispatcher
        # A cloud run is not given a workspace base; its sandbox has /workspace.
        self.workspace = Path(os.environ.get("WORKSPACE_BASE", "/workspace"))
        self.project = self.workspace
        self.evidence = self.workspace / "evidence"
        self.evidence.mkdir(exist_ok=True)
        self._completed_dependencies = {}

    @cached_property
    def base_branch(self):
        return self.config.get("base_branch") or self.gh("GET", "")["default_branch"]

    def base_sha(self, branch: str) -> str:
        """The commit at the tip of ``branch``.

        GitHub answers ``/git/ref/heads/<branch>`` with one object; Gitea
        answers ``/git/refs/heads/<branch>`` with a list. Both are normalized
        to the SHA the prompt names.
        """
        if self.forge == "gitea":
            refs = self.gh("GET", f"/git/refs/heads/{branch}")
            if isinstance(refs, list):
                if not refs:
                    raise RuntimeError(f"Branch {branch!r} was not found")
                refs = refs[0]
            return refs["object"]["sha"]
        return self.gh("GET", f"/git/ref/heads/{branch}")["object"]["sha"]

    @property
    def github_instructions(self):
        # A cloud conversation runs in a sandbox of its own, so this run's
        # workspace path means nothing to it.
        where = "your working directory" if is_cloud_run() else self.project
        if self.forge == "github":
            access = f"Use `GH_TOKEN=${self.token_name} gh api` for GitHub requests."
        else:
            access = (
                f"Use `curl -H \"Authorization: token ${self.token_name}\" "
                f"{self.api_base}` for {self.forge} API requests."
            )
        return (
            f"{access} Never print the credential value. "
            f"Only {self.repository} is in scope. Work in {where}. "
            "Do not modify the automation bundle or its configuration."
        )

    def _request(self, method, path, params=None, body=None):
        return github_request(
            self.token,
            method,
            path,
            params=params,
            body=body,
            accept=self.accept,
            api_base=self.api_base,
            token_style=self.token_style,
        )

    def gh(self, method, path, body=None):
        return self._request(method, f"/repos/{self.repository}" + path, body=body)[0]

    def api(self, method, path, params=None, body=None):
        """Call a forge endpoint that is not scoped to one repository."""
        return self._request(method, path, params=params, body=body)[0]

    def shell(self, args, cwd=None, timeout=300):
        result = subprocess.run(
            args,
            cwd=cwd or self.project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"{args[0]} failed: {result.stdout[-4000:].replace(self.token, '[REDACTED]')}"
            )
        return result.stdout.strip()

    def comment(self, number, text):
        return self.gh(
            "POST",
            f"/issues/{number}/comments",
            {
                "body": text
                + f"\n\nFactory role: `{self.name}`; conversation: `{self.conversation_id}`."
                + "\n\n_This comment was posted by an AI agent (OpenHands)._"
            },
        )

    def open_issues(self):
        # GitHub omits `pull_request` on issues; Gitea includes it as null.
        return [i for i in self.gh_pages("/issues?state=open") if not i.get("pull_request")]

    def statuses(self, sha):
        result = {}
        for item in self.gh_pages(f"/commits/{sha}/statuses"):
            # GitHub calls the outcome "state"; Gitea calls it "status".
            result.setdefault(
                item["context"], item.get("state") or item.get("status", "")
            )
        return result

    def _mention_scan(self, pr_number):
        """Return PR mentions and the timestamp of the latest bot reply."""
        items = []
        bot_replies = []
        bot_username = (self.config.get("bot_username") or "").lower()

        def add(item, kind, *, path=None, line=None):
            body = item.get("body") or ""
            author = ((item.get("user") or {}).get("login") or "").lower()
            if (bot_username and author == bot_username) or any(
                marker in body for marker in BOT_REPLY_MARKERS
            ):
                bot_replies.append(_item_time(item))
                return
            if not OPENHANDS_MENTION_RE.search(body):
                return
            items.append(
                {
                    "kind": kind,
                    "id": item.get("id"),
                    "body": body,
                    "html_url": item.get("html_url") or "",
                    "path": path or item.get("path"),
                    "line": line if line is not None else item.get("line"),
                    "author": (item.get("user") or {}).get("login", ""),
                    "created_at": item.get("created_at")
                    or item.get("submitted_at")
                    or "",
                }
            )

        for comment in self.gh_pages(f"/issues/{pr_number}/comments"):
            add(comment, "comment")

        reviews = self.gh_pages(f"/pulls/{pr_number}/reviews")
        for review in reviews:
            add(review, "review")
            if self.forge == "github":
                continue
            # Gitea exposes inline review comments per review, not per PR.
            for comment in self.gh_pages(
                f"/pulls/{pr_number}/reviews/{review['id']}/comments"
            ):
                add(
                    comment,
                    "review_comment",
                    path=comment.get("path"),
                    line=comment.get("line") or comment.get("position"),
                )

        if self.forge == "github":
            for comment in self.gh_pages(f"/pulls/{pr_number}/comments"):
                add(
                    comment,
                    "review_comment",
                    path=comment.get("path"),
                    line=comment.get("line") or comment.get("original_line"),
                )

        items.sort(
            key=lambda item: (
                _item_time(item),
                str(item["kind"]),
                str(item["id"]),
            )
        )
        return items, max(bot_replies, default=0.0)

    def _mention_items(self, pr_number):
        return self._mention_scan(pr_number)[0]

    def latest_openhands_mention(self, pr_number):
        """The latest reviewer mention posted after the bot's last reply."""
        items, last_bot_reply = self._mention_scan(pr_number)
        fresh = [item for item in items if _item_time(item) > last_bot_reply]
        return fresh[-1] if fresh else None

    def check_runs(self, sha):
        """Return every check run GitHub reported for one commit SHA.

        Reading check runs needs no branch-protection or ruleset access, so this
        is the head-eligibility signal the automation's own token can always
        see. The endpoint answers with an object rather than a list, so it
        paginates through ``gh`` instead of ``gh_pages``.
        """
        if self.forge != "github":
            return []
        runs = []
        for page in range(1, 101):
            data = self.gh(
                "GET", f"/commits/{sha}/check-runs?per_page=100&page={page}"
            )
            batch = data.get("check_runs") or []
            runs.extend(batch)
            if len(runs) >= int(data.get("total_count") or 0) or not batch:
                return runs
        raise RuntimeError("GitHub check-run pagination exceeded limit")

    def required_check_contexts(self, number):
        """Return the status contexts GitHub marks required on one pull request.

        `isRequired` is the merge-policy source of truth and is PR-scoped, so it
        is correct for a stacked PR whose symbolic base branch carries no branch
        rules of its own. It is a field on each context in the head's status
        check rollup and takes the pull request number, so a single GraphQL query
        answers with the required check runs and any required commit statuses,
        each already attributed to the exact head. Only contexts GitHub itself
        reports as required are returned, so an optional workflow that fails
        before creating any check run is not present.
        """
        owner, name = self.repository.split("/", 1)
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){"
            "pullRequest(number:$number){"
            "commits(last:1){"
            "nodes{"
            "commit{"
            "statusCheckRollup{"
            "contexts(first:100){"
            "pageInfo{hasNextPage}"
            "nodes{"
            "__typename"
            " ... on CheckRun{name isRequired(pullRequestNumber:$number)}"
            " ... on StatusContext{context isRequired(pullRequestNumber:$number)}"
            "}"
            "}"
            "}"
            "}"
            "}"
            "}"
            "}"
            "}"
            "}"
        )
        data = self.api(
            "POST",
            "/graphql",
            body={
                "query": query,
                "variables": {"owner": owner, "name": name, "number": number},
            },
        )
        if data.get("errors"):
            raise RuntimeError(
                f"GitHub required-check query failed: {data['errors'][0].get('message')}"
            )
        nodes = (
            (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
            .get("commits", {})
            .get("nodes")
        ) or []
        contexts = (
            ((nodes[0].get("commit") or {}).get("statusCheckRollup") or {}).get(
                "contexts", {}
            )
            if nodes
            else {}
        )
        if (contexts.get("pageInfo") or {}).get("hasNextPage"):
            raise RuntimeError(
                "GitHub required-check rollup exceeds one page; the required "
                "set may be incomplete"
            )
        return [
            {
                "name": node.get("name") or node.get("context"),
                "kind": node.get("__typename"),
            }
            for node in (contexts.get("nodes") or [])
            if node.get("isRequired") and (node.get("name") or node.get("context"))
        ]

    def workflow_runs(self, sha):
        """Return every Actions workflow run GitHub reported for one commit SHA.

        A workflow that fails before any job starts - a workflow-level error, or
        a `pull_request` run whose jobs never materialize - still records a
        failed check suite, but it contributes no check runs, so the commit's
        check-run rollup and `gh pr checks` both report success. Reading the
        workflow runs directly is the only way the gate can see that failure.
        Like the check-run endpoint this answers with an object, so it paginates
        manually.
        """
        if self.forge != "github":
            return []
        runs = []
        for page in range(1, 101):
            data = self.gh(
                "GET", f"/actions/runs?head_sha={sha}&per_page=100&page={page}"
            )
            batch = data.get("workflow_runs") or []
            runs.extend(batch)
            if len(runs) >= int(data.get("total_count") or 0) or not batch:
                return runs
        raise RuntimeError("GitHub workflow-run pagination exceeded limit")

    def completed_dependency(self, number):
        if number in self._completed_dependencies:
            return self._completed_dependencies[number]
        try:
            dependency = self.gh("GET", f"/issues/{number}")
        except HTTPError as exc:
            if exc.code == 404:
                return False
            raise
        completed = dependency["state"] == "closed" and (
            self.forge != "github"
            or dependency.get("state_reason") == "completed"
        )
        self._completed_dependencies[number] = completed
        return completed

    def dependencies_complete(self, issue):
        """Honor explicit Depends on lines; unknown/incomplete issues remain blocked."""
        for line in re.findall(
            "^Depends on:\\s*(.+)$",
            issue.get("body") or "",
            re.MULTILINE | re.IGNORECASE,
        ):
            for number in re.findall("#(\\d+)", line):
                if not self.completed_dependency(number):
                    return False
        return True

    def gh_pages(self, endpoint):
        split = urlsplit(endpoint)
        return github_paginate(
            self.token,
            f"/repos/{self.repository}" + split.path,
            params=dict(parse_qsl(split.query)),
            api_base=self.api_base,
            token_style=self.token_style,
            page_size_param="limit" if self.forge == "gitea" else "per_page",
            page_size=50 if self.forge == "gitea" else 100,
        )


def run_repositories(automation_type, conversation=None, dispatcher=None):
    parser = argparse.ArgumentParser(description=automation_type.__doc__)
    parser.add_argument("--github-token-secret")
    args = parser.parse_args()
    config = json.loads(Path("config.json").read_text())
    token_name = args.github_token_secret or config.get(
        "github_token_secret", "GITHUB_PERSONAL_ACCESS_TOKEN"
    )
    repositories = config.get("repos") or [config["repository"]]
    failures = []
    for repository in repositories:
        options = dict(
            github_token_secret=token_name,
            repository=repository,
            conversation=conversation,
        )
        if dispatcher is not None:
            options["dispatcher"] = dispatcher
        automation = automation_type(**options)
        try:
            automation.run()
        except Exception as exc:  # noqa: BLE001 - one repository must not block others
            failures.append(repository)
            print(
                json.dumps({"repository": repository, "error": type(exc).__name__}),
                flush=True,
            )
    if failures:
        raise RuntimeError("Automation failed for: " + ", ".join(failures))
    return str(conversation.id) if conversation else None
