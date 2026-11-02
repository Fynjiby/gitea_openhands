"""Select ready GitHub work and delegate each subject to a sandboxed agent."""

import json
import re

import main as workflow
from agent_conversation import AgentConversationDispatcher
from github_client import GitHubRepository, is_cloud_run, run_repositories


class IssueToPR(GitHubRepository):
    name = "github-issue-to-pr"

    def _workspace_instructions(self, branch, revision):
        token = self.token_name
        if revision:
            checkout = f"check out the existing remote branch `{branch}`"
        else:
            checkout = (
                f"create and check out `{branch}` from the repository's base branch"
            )
        if self.forge == "github":
            auth_url = (
                f"https://x-access-token:${{{token}}}@github.com/"
                f"{self.repository}.git"
            )
        else:
            auth_url = (
                f"https://oauth2:${{{token}}}@{self.host}/{self.repository}.git"
            )
        if is_cloud_run():
            lead = "The workspace is a fresh directory."
        else:
            # The run workspace is where this bundle was extracted, so it already
            # contains main.py, worker.py and config.json. Cloning into `.` would
            # fail; clone beside them and leave them untouched.
            lead = (
                "The workspace also contains this automation's own scripts; do not "
                "modify or delete them."
            )
        return (
            f"{lead} Clone the repository into a new directory `repo` with "
            f"`git clone {auth_url} repo`, then work inside `repo` and {checkout}. "
            f"After cloning, run `git remote set-url origin {self.clone_url}` there "
            "so the remote keeps no embedded credential."
        )

    def _prompt(
        self, issue, branch, base_branch, base_sha, *, revision=None, mention=None
    ):
        review_label = self.config.get("review_label", "openhands-review")
        prompt = workflow._build_implementation_prompt(
            self.repository,
            issue,
            {
                "id": issue.get("updated_at", "?"),
                "created_at": issue.get("updated_at", "?"),
            },
            branch,
            base_branch,
            base_sha,
            workspace_instructions=self._workspace_instructions(branch, revision),
            github_token_secret=self.token_name,
            forge=self.forge,
            clone_url=self.clone_url,
            web_url=self.web_url,
            api_url=self.api_base,
        )
        if revision:
            mention_text = ""
            if mention:
                quoted = "\n".join(
                    f"> {line}" for line in (mention.get("body") or "").splitlines()
                )
                location = ""
                if mention.get("path"):
                    location = (
                        f" at `{mention['path']}:{mention.get('line') or '?'}`"
                    )
                mention_text = (
                    "\n\nTriggering mention"
                    f"{location}:\n\n{quoted}\n\n"
                    f"Link: {mention.get('html_url') or '(not available)'}\n"
                )
            prompt = (
                f"Revise existing PR #{revision['number']} at exact head "
                f"`{revision['head']['sha']}`. A reviewer mentioned `@openhands`; "
                "read the full discussion, including the triggering mention and any "
                "inline comments, and address each current finding or explain with "
                "evidence why no code change is warranted. Push revisions to the "
                "existing branch; do not open another pull request. Reply on the "
                "pull request summarizing what you changed or why no change is "
                "needed. Include the exact marker `<!-- openhands-reply -->` in "
                "every reply and do not write `@openhands` in your replies. After "
                f"replying, remove and reapply the `{review_label}` label so the "
                "exact new head is reviewed.\n"
                + mention_text
                + "\n\n"
                + prompt
            )
        else:
            prompt += (
                f"\n\nAfter opening the pull request, add the `{review_label}` label so "
                "the independent reviewer checks its exact head."
            )
        return prompt

    def _submit(
        self,
        repository_id,
        issue,
        branch,
        base_branch,
        base_sha,
        *,
        revision=None,
        revision_key=None,
        mention=None,
    ):
        revision_sha = revision["head"]["sha"] if revision else None
        key = (
            revision_key
            or revision_sha
            or issue.get("updated_at")
            or str(issue["number"])
        )
        result = self.dispatcher.deliver(
            subject=f"{repository_id}:issue:{issue['number']}",
            delivery=f"{issue['number']}:{key}",
            prompt=self._prompt(
                issue,
                branch,
                base_branch,
                revision_sha or base_sha,
                revision=revision,
                mention=mention,
            ),
            head=revision_sha or "",
            secrets={self.token_name: self.token},
        )
        print(
            json.dumps(
                {
                    "repository": self.repository,
                    "issue": issue["number"],
                    "pr": revision["number"] if revision else None,
                    "disposition": result["disposition"],
                    "conversation_id": result["conversation_id"],
                }
            ),
            flush=True,
        )

    def _try_submit(
        self,
        repository_id,
        issue,
        branch,
        base_branch,
        base_sha,
        *,
        revision=None,
        revision_key=None,
        mention=None,
    ):
        try:
            self._submit(
                repository_id,
                issue,
                branch,
                base_branch,
                base_sha,
                revision=revision,
                revision_key=revision_key,
                mention=mention,
            )
        except Exception as exc:  # noqa: BLE001 - one issue must not block the scan
            print(
                f"Failed to submit {self.repository} issue "
                f"#{issue.get('number', '?')}: {exc}",
                flush=True,
            )

    def run(self):
        trigger_label = self.config.get("trigger_label", workflow.TRIGGER_LABEL)
        branch_prefix = self.config.get("branch_prefix", workflow.BRANCH_PREFIX)
        repository = self.gh("GET", "")
        repository_id = repository["id"]
        base_branch = self.config.get("base_branch") or repository["default_branch"]
        base_sha = self.base_sha(base_branch)
        issues = {issue["number"]: issue for issue in self.open_issues()}

        open_prs = self.gh_pages("/pulls?state=open")
        issue_prs = {}
        for pr in open_prs:
            match = re.fullmatch(
                re.escape(branch_prefix) + r"-(\d+)", pr["head"]["ref"]
            )
            if match:
                issue_prs[int(match[1])] = pr

        for issue_number, pr in sorted(issue_prs.items()):
            issue = issues.get(issue_number)
            if issue is None:
                continue
            try:
                mention = self.latest_openhands_mention(pr["number"])
            except Exception as exc:  # noqa: BLE001 - one PR must not block the scan
                print(
                    f"Failed to read mentions on {self.repository} PR "
                    f"#{pr['number']}: {exc}",
                    flush=True,
                )
                continue
            if mention is None:
                continue
            self._try_submit(
                repository_id,
                issue,
                pr["head"]["ref"],
                (pr.get("base") or {}).get("ref") or base_branch,
                base_sha,
                revision=pr,
                revision_key=f"mention:{mention['kind']}:{mention['id']}",
                mention=mention,
            )

        ready = [
            issue
            for issue in issues.values()
            if issue["number"] not in issue_prs
            and trigger_label in {label["name"] for label in issue.get("labels", [])}
            and self.dependencies_complete(issue)
        ]
        for issue in sorted(
            ready,
            key=lambda item: (
                "priority:high"
                not in {label["name"] for label in item.get("labels", [])},
                item["number"],
            ),
        ):
            self._try_submit(
                repository_id,
                issue,
                f"{branch_prefix}-{issue['number']}",
                base_branch,
                base_sha,
            )


if __name__ == "__main__":
    with AgentConversationDispatcher() as dispatcher:
        run_repositories(IssueToPR, dispatcher=dispatcher)
