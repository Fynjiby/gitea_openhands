# OpenHands automation export: labeled issue → pull request

A portable export of a cron-driven OpenHands automation. It polls one forge
repository for open issues carrying a trigger label, starts an agent
conversation that implements each issue and opens a pull request, and starts a
revision conversation whenever a reviewer mentions `@openhands` on one of its
pull requests.

It works with Gitea and GitHub.

The export contains no credentials and no deployment-specific values. Every
repository, host, label, secret name and automation name is asked for at import
time.

## Layout

| Path | Purpose |
| --- | --- |
| `manifest.json` | The automation definition (trigger, entrypoint, timeout, state, tags, model) plus the questions the importer asks |
| `config.template.json` | The bundle's runtime config, with `${placeholders}` |
| `bundle/` | Automation code: `worker.py` (entrypoint), `main.py`, `github_client.py`, `agent_conversation.py` |
| `import_automation.py` | Asks for every value, packs the bundle, uploads it and creates the automation |

`bundle/config.json` is generated during import and is not committed.

## Before importing

In the target OpenHands instance:

- The automation backend is reachable, and you have a session API key for it.
- A secret holding the forge token is registered under a name you can supply
  (for example `GITEA_TOKEN` or `GITHUB_TOKEN`). The importer asks for the
  **name**, never the value. The token needs permission to read issues and pull
  requests and to push branches in the target repository.
- The `@openhands` handle the automation replies to must be the one the reviewer
  mentions on pull requests.
- Optionally, a model profile or agent profile id to run with.

The automation API base URL is typically `<instance>/api/automation/v1`, and the
key is sent in the `X-Session-API-Key` header. Ask the user for both instead of
guessing.

## Import

```bash
export OPENHANDS_AUTOMATION_API_KEY=<session-key>
python3 import_automation.py --api-url <automation-api-base>
```

The script asks, in order, for everything in `manifest.json`: repository, forge,
API URL, base branch, trigger label, review label, branch prefix, pull request
mode, bot username, token secret name, run limits, automation name and
description, schedule, timezone, timeout, state, tags, model and agent profile.

It then renders `config.json` into a copy of `bundle/`, tars it, uploads it and
creates the automation. Nothing is written outside a temporary directory.

Useful flags:

- `--dry-run` — print the rendered bundle config and create payload, create
  nothing.
- `--validate` — validate the draft against the backend before uploading.
- `--dispatch` — trigger one run after creation.
- `--answers answers.json` — non-interactive answers.
- `--set key=value` — answer individual parameters (repeatable).
- `OA_PARAM_<KEY>` environment variables are also accepted.

Answers files can hold only the keys they need; anything missing is asked for
interactively or taken from the manifest default.

## Notes for the agent doing the import

1. Ask the user for the automation API URL, the session API key, and every
   value the script prompts for. Do not infer a repository, host, label, secret
   name, schedule or automation name from this document.
2. Never ask the user to paste a forge token into this repository, an answers
   file, a command line or the chat. Ask them to register it as a secret in
   OpenHands and give you its **name**.
3. Run `--dry-run` first and show the rendered config to the user.
4. Create the automation in `INACTIVE` state (the manifest default) unless the
   user asks for it to run immediately, then enable it.
5. After creation, report the automation id, its trigger and its state.

## Security

- The export contains no token values. The config carries a secret name; the
  token is resolved by the OpenHands runtime at run time.
- `answers.json` and `bundle/config.json` are git-ignored because they hold
  deployment names, not credentials.
- The original automation's preset metadata embedded a pasted token. It was
  deliberately left out of this export; the token must be registered as a secret
  in the target instance and supplied by name.
