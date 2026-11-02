#!/usr/bin/env python3
"""Create this automation in an OpenHands instance.

Every value that belongs to a deployment comes from the user: answers file,
--set flags, OA_PARAM_* environment variables, or an interactive prompt. The
script resolves nothing on its own except paths relative to itself.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
SKIPPED_DIRS = {"__pycache__"}


def load_manifest():
    return json.loads((ROOT / "manifest.json").read_text())


def coerce(param, raw):
    kind = param.get("kind", "string")
    if kind == "int":
        try:
            value = int(raw)
        except ValueError:
            raise ValueError("Expected a whole number")
        if param["key"] == "timeout" and not 1 <= value <= 1800:
            raise ValueError("Timeout must be between 1 and 1800 seconds")
        if param["key"] == "max_new_per_run" and value < 1:
            raise ValueError("Must be at least 1")
        return value
    if kind == "list":
        return [item.strip() for item in str(raw).split(",") if item.strip()]
    value = str(raw).strip()
    if kind == "choice" and value not in param["choices"]:
        raise ValueError("Choose one of: " + ", ".join(param["choices"]))
    pattern = param.get("pattern")
    if pattern and not re.fullmatch(pattern, value):
        raise ValueError(param.get("pattern_error", "Value does not match " + pattern))
    return value


def parse_sets(items):
    values = {}
    for item in items or []:
        key, sep, raw = item.partition("=")
        if not sep:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        values[key.strip()] = raw
    return values


def ask(param, default):
    suffix = ""
    if param.get("kind") == "choice":
        suffix += " (" + "|".join(param["choices"]) + ")"
    if default is not None:
        suffix += f" [default: {default}]"
    elif not param.get("required"):
        suffix += " [blank to skip]"
    while True:
        try:
            raw = input(f"{param['prompt']}{suffix}: ").strip()
        except EOFError:
            raise SystemExit(
                "\nInput closed before every value was provided; "
                "pass --answers or --set for the rest"
            )
        if not raw:
            if default is not None:
                return default
            if not param.get("required"):
                return None
            print("  this value is required")
            continue
        try:
            return coerce(param, raw)
        except ValueError as exc:
            print(f"  {exc}")


def collect(manifest, answers_path, sets):
    supplied = {}
    if answers_path:
        supplied.update(json.loads(Path(answers_path).read_text()))
    supplied.update(parse_sets(sets))

    values = {}
    missing = []
    for param in manifest["parameters"]:
        key = param["key"]
        if supplied.get(key) not in (None, ""):
            values[key] = coerce(param, supplied[key])
            continue
        from_env = os.environ.get("OA_PARAM_" + key.upper())
        if from_env:
            values[key] = coerce(param, from_env)
            continue
        default = param.get("default")
        if not sys.stdin.isatty():
            if default is not None:
                values[key] = default
            elif not param.get("required"):
                values[key] = None
            else:
                missing.append(key)
            continue
        values[key] = ask(param, default)

    if missing:
        raise SystemExit(
            "Not running interactively and no value given for: "
            + ", ".join(missing)
            + ". Pass --answers, --set key=value or OA_PARAM_<KEY>."
        )
    return values


def resolve(node, values):
    if isinstance(node, dict):
        return {key: resolve(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [resolve(item, values) for item in node]
    if isinstance(node, str):
        whole = PLACEHOLDER.fullmatch(node)
        if whole:
            return values.get(whole.group(1))
        return PLACEHOLDER.sub(lambda m: str(values.get(m.group(1))), node)
    return node


def prune(node):
    if isinstance(node, dict):
        return {
            key: prune(value)
            for key, value in node.items()
            if value not in (None, "")
        }
    if isinstance(node, list):
        return [prune(item) for item in node]
    return node


def stage_bundle(manifest, config, target):
    source = ROOT / manifest["bundle"]["directory"]
    shutil.copytree(
        source, target, ignore=shutil.ignore_patterns(*SKIPPED_DIRS)
    )
    config_name = manifest["bundle"]["config_filename"]
    (target / config_name).write_text(json.dumps(config, indent=2) + "\n")


def build_tarball(manifest, config, out_path):
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "bundle"
        stage_bundle(manifest, config, staged)
        with tarfile.open(out_path, "w:gz") as tar:
            for entry in sorted(staged.iterdir()):
                tar.add(entry, arcname=entry.name)


def api_call(api_url, api_key, method, path, *, body=None, data=None, content_type=None):
    headers = {"X-Session-API-Key": api_key, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        api_url.rstrip("/") + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"{method} {path} failed with {exc.code}: {detail}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url",
        default=os.environ.get("OPENHANDS_AUTOMATION_URL"),
        help="Automation API base URL, e.g. https://host/api/automation/v1 "
        "(or OPENHANDS_AUTOMATION_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENHANDS_AUTOMATION_API_KEY"),
        help="API key sent as X-Session-API-Key (or OPENHANDS_AUTOMATION_API_KEY)",
    )
    parser.add_argument("--answers", help="JSON file with parameter values")
    parser.add_argument(
        "--set", action="append", metavar="KEY=VALUE", help="Answer one parameter"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Render and print, create nothing"
    )
    parser.add_argument(
        "--validate", action="store_true", help="Validate the draft before uploading"
    )
    parser.add_argument(
        "--dispatch", action="store_true", help="Dispatch one run after creation"
    )
    args = parser.parse_args()

    if not args.api_url or not args.api_key:
        parser.error(
            "--api-url/--api-key (or OPENHANDS_AUTOMATION_URL/"
            "OPENHANDS_AUTOMATION_API_KEY) are required; ask the user for them"
        )

    manifest = load_manifest()
    values = collect(manifest, args.answers, args.set)
    config = prune(resolve(
        json.loads((ROOT / manifest["bundle"]["config_template"]).read_text()), values
    ))
    payload = prune(resolve(manifest["create"], values))
    payload["tarball_path"] = "oh-internal://uploads/00000000-0000-0000-0000-000000000000"

    print(json.dumps({"config": config, "create": payload}, indent=2))
    if args.dry_run:
        return

    if args.validate:
        _, result = api_call(
            args.api_url,
            args.api_key,
            "POST",
            "/validate",
            body={"endpoint": "/v1", "draft": payload},
        )
        if not result.get("valid"):
            raise SystemExit("Draft is invalid:\n" + json.dumps(result, indent=2))
        print("Draft is valid")

    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "bundle.tar.gz"
        build_tarball(manifest, config, tarball)
        query = urllib.parse.urlencode({"name": payload["name"] + " bundle"})
        _, upload = api_call(
            args.api_url,
            args.api_key,
            "POST",
            "/uploads?" + query,
            data=tarball.read_bytes(),
            content_type="application/gzip",
        )
    payload["tarball_path"] = upload["tarball_path"]

    _, automation = api_call(args.api_url, args.api_key, "POST", "", body=payload)
    print(
        json.dumps(
            {
                "id": automation["id"],
                "name": automation["name"],
                "state": automation.get("state"),
                "trigger": automation.get("trigger"),
            },
            indent=2,
        )
    )

    if args.dispatch:
        api_call(args.api_url, args.api_key, "POST", f"/{automation['id']}/dispatch")
        print("Dispatched one run")


if __name__ == "__main__":
    main()
