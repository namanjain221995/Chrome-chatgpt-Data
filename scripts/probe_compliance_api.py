#!/usr/bin/env python3
"""Probe the OpenAI Enterprise Compliance API and report what it returns.

This is a discovery tool, not part of the running system. It answers the
question the production adapter cannot be configured without:

    does this feed return message content, or only event metadata?

It deliberately guesses **nothing**. The base URL and the log path come from
the API documentation supplied with your Enterprise agreement; without them the
script stops and says so, rather than inventing a path that would fail in a way
that looks like "no data".

    cp .env.compliance.example .env.compliance   # then fill it in
    python3 scripts/probe_compliance_api.py

Standard library only, so it runs without installing anything.

The token is never printed, never written to the output directory, and never
included in an error message.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env.compliance"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "compliance-probe"

REQUIRED = ("OPENAI_COMPLIANCE_BASE_URL", "OPENAI_COMPLIANCE_LOG_PATH", "OPENAI_COMPLIANCE_API_KEY")

#: Field names that would indicate the payload carries actual conversation text
#: rather than only identifiers. Used for reporting, never for parsing.
CONTENT_HINTS = {
    "content",
    "text",
    "body",
    "message",
    "messages",
    "parts",
    "prompt",
    "completion",
    "response",
    "conversation",
}


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader. No interpolation, no export, no surprises."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def redact(value: Any, *, keep: int = 6) -> str:
    """Describe a value's shape without reproducing employee content."""
    if isinstance(value, str):
        shown = value[:keep].replace("\n", " ")
        return f"str(len={len(value)}) {shown!r}…" if len(value) > keep else f"str {value!r}"
    if isinstance(value, bool):
        return f"bool {value}"
    if isinstance(value, (int, float)):
        return f"{type(value).__name__} {value}"
    if isinstance(value, list):
        inner = f", first={redact(value[0], keep=keep)}" if value else ""
        return f"list(len={len(value)}{inner})"
    if isinstance(value, dict):
        return f"object(keys={sorted(value)[:12]})"
    if value is None:
        return "null"
    return type(value).__name__


def describe(node: Any, prefix: str = "", depth: int = 0, out: list[str] | None = None) -> list[str]:
    """Flatten a JSON document into dotted paths with redacted values."""
    out = [] if out is None else out
    if depth > 4:
        return out
    if isinstance(node, dict):
        for key in sorted(node):
            describe(node[key], f"{prefix}.{key}" if prefix else key, depth + 1, out)
    elif isinstance(node, list):
        out.append(f"{prefix or '<root>'} = list(len={len(node)})")
        if node:
            describe(node[0], f"{prefix}[0]", depth + 1, out)
    else:
        out.append(f"{prefix or '<root>'} = {redact(node)}")
    return out


def request_json(
    url: str,
    headers: dict[str, str],
    timeout: int,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, dict[str, str], Any, bytes]:
    headers = dict(headers)
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(  # noqa: S310 - configured URL
        url, headers=headers, method=method, data=body
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            status = response.status
            resp_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
    except urllib.error.URLError as exc:
        # Never let a token reach an error string.
        raise SystemExit(f"could not reach the host: {exc.reason}") from None

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, resp_headers, parsed, body


def dotted(document: Any, path: str) -> Any:
    node = document
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--path",
        help="override OPENAI_COMPLIANCE_LOG_PATH for this run",
    )
    parser.add_argument(
        "--try-paths",
        metavar="P1,P2,...",
        help=(
            "probe several candidate paths and report the status of each, "
            "without saving anything. Use this to identify the documented "
            "endpoint empirically: 404 means wrong path, 200 means it exists."
        ),
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--max-pages", type=int, default=2, help="pages to follow (default 2)")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--method", default="GET", help="HTTP method (default GET)")
    parser.add_argument(
        "--body",
        help="JSON request body, for endpoints that take a POST query document",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra query parameter; repeatable",
    )
    parser.add_argument(
        "--describe-only",
        action="store_true",
        help="print the response shape and save nothing",
    )
    args = parser.parse_args()

    # Diagnostics are worthless if they arrive out of order: when output is
    # piped, block-buffered stdout would appear after unbuffered stderr, so the
    # explanation of a 404 would print above the 404 itself.
    sys.stdout.reconfigure(line_buffering=True)

    env = {**load_env_file(args.env_file), **os.environ}

    required = list(REQUIRED)
    if args.path or args.try_paths:
        # The path is supplied on the command line for this run.
        required = [name for name in required if name != "OPENAI_COMPLIANCE_LOG_PATH"]

    missing = [name for name in required if not env.get(name)]
    if missing:
        print("Cannot probe: these values are not set.\n", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print(
            "\nThe base URL and log path come from the Compliance API documentation\n"
            "supplied with your OpenAI Enterprise agreement. This script does not\n"
            "guess them: a wrong path returns an error that looks exactly like\n"
            "'there is no data', which is the worst possible failure here.\n\n"
            f"Copy {args.env_file.name}.example and fill it in, or ask your OpenAI\n"
            "account contact for the Compliance API documentation.",
            file=sys.stderr,
        )
        return 2

    base = env["OPENAI_COMPLIANCE_BASE_URL"].rstrip("/")
    path = "/" + (args.path or env.get("OPENAI_COMPLIANCE_LOG_PATH", "")).lstrip("/")
    token = env["OPENAI_COMPLIANCE_API_KEY"]

    header_name = env.get("OPENAI_COMPLIANCE_AUTH_HEADER", "Authorization")
    scheme = env.get("OPENAI_COMPLIANCE_AUTH_SCHEME", "Bearer").strip()
    headers = {
        header_name: f"{scheme} {token}".strip(),
        "Accept": "application/json",
        "User-Agent": "techsara-chat-archive-probe/1.0",
    }
    if org := env.get("OPENAI_ORG_ID"):
        headers["OpenAI-Organization"] = org

    params: dict[str, str] = {}
    if raw_query := env.get("OPENAI_COMPLIANCE_QUERY", "").strip():
        try:
            params.update(json.loads(raw_query))
        except json.JSONDecodeError:
            print("OPENAI_COMPLIANCE_QUERY must be a JSON object", file=sys.stderr)
            return 2
    if workspace := env.get("OPENAI_WORKSPACE_ID"):
        params.setdefault("workspace_id", workspace)
    for item in args.param:
        key, _, value = item.partition("=")
        params[key] = value

    items_path = env.get("OPENAI_COMPLIANCE_ITEMS_FIELD", "data")
    cursor_path = env.get("OPENAI_COMPLIANCE_CURSOR_FIELD", "next_cursor")
    has_more_path = env.get("OPENAI_COMPLIANCE_HAS_MORE_FIELD", "has_more")
    cursor_param = env.get("OPENAI_COMPLIANCE_CURSOR_PARAM", "after")

    request_body: bytes | None = None
    if args.body:
        try:
            json.loads(args.body)
        except json.JSONDecodeError:
            print("--body must be valid JSON", file=sys.stderr)
            return 2
        request_body = args.body.encode("utf-8")

    saving = not (args.describe_only or args.try_paths)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir / stamp
    if saving:
        out_dir.mkdir(parents=True, exist_ok=True)

    if args.try_paths:
        print(f"host      {base}")
    else:
        print(f"endpoint  {base}{path}")
    print(f"auth      {header_name}: {scheme} <token hidden, {len(token)} chars>")
    if params:
        print(f"query     {params}")
    print(f"output    {out_dir if saving else '(nothing saved)'}")
    print()

    if args.try_paths:
        print("Testing candidate paths. Nothing is saved and nothing is assumed:")
        print("  200 = the endpoint exists      404 = wrong path, not empty data")
        print("  401/403 = credential rejected  other = read the message\n")
        found: list[str] = []
        for candidate in [c.strip() for c in args.try_paths.split(",") if c.strip()]:
            candidate_path = "/" + candidate.lstrip("/")
            url = f"{base}{candidate_path}"
            if params:
                url = f"{url}?{urllib.parse.urlencode(params)}"
            status, _, document, body = request_json(
                url, headers, args.timeout, args.method, request_body
            )
            method_used = args.method

            # 405 is the most informative answer this API gives: the path
            # exists, only the verb is wrong. Retry it rather than reporting a
            # real endpoint as a failure.
            if status == 405 and args.method == "GET":
                post_status, _, post_doc, post_body = request_json(
                    url, headers, args.timeout, "POST", request_body or b"{}"
                )
                if post_status != 405:
                    status, document, body = post_status, post_doc, post_body
                    method_used = "POST"

            note = ""
            if status == 200 and isinstance(document, dict):
                items = dotted(document, items_path)
                note = (
                    f"  items at {items_path!r}: {len(items)}"
                    if isinstance(items, list)
                    else f"  top-level keys: {sorted(document)[:8]}"
                )
                found.append(f"{method_used} {candidate_path}")
            elif status == 405:
                note = "  PATH EXISTS, wrong method - try --method POST with --body"
            elif status in (401, 403):
                detail = body[:200].decode("utf-8", "replace")
                if "access_enforcement" in detail or "no_matching_rule" in detail:
                    note = "  token recognised but NOT AUTHORISED for this endpoint"
                else:
                    note = "  credential rejected"
            elif status >= 400:
                note = f"  {body[:100].decode('utf-8', 'replace')}"
            print(f"  {status:>3}  {method_used:<4} {candidate_path}{note}")

        print()
        if found:
            print("Endpoints that responded 200:")
            for hit in found:
                print(f"  {hit}")
            first_method, _, first_path = found[0].partition(" ")
            body_flag = " --body '{}'" if first_method == "POST" else ""
            print("\nRe-run against one of them to see its shape:")
            print(
                f"  python3 scripts/{Path(__file__).name} --path '{first_path}'"
                f" --method {first_method}{body_flag} --describe-only"
            )
            print("\nConfirm it against the documentation before putting it in SSM.")
        else:
            print("None responded 200. That does not mean the API is unavailable:")
            print("  * every 404 means these particular paths are wrong;")
            print("  * a 401/403 means the token is not accepted at all.")
            print("Get the documented path from your OpenAI account contact.")
        return 0 if found else 1

    total_items = 0
    cursor: str | None = None

    for page_number in range(1, args.max_pages + 1):
        query = dict(params)
        if cursor:
            query[cursor_param] = cursor
        url = f"{base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        status, resp_headers, document, body = request_json(
            url, headers, args.timeout, args.method, request_body
        )
        print(f"--- page {page_number}: HTTP {status}, {len(body)} bytes ---")

        if status == 401 or status == 403:
            print(
                "\nThe credential was rejected. Check that the token is a Compliance API\n"
                "key and that it has not expired. Nothing about the token is printed here.",
                file=sys.stderr,
            )
            return 1
        if status == 405:
            print(
                f"\n405 means {path!r} EXISTS but does not accept {args.method}.\n"
                "This is a strong signal you have the right path. Try:\n"
                f"  python3 {Path(__file__).name} --path '{path}' --method POST "
                "--body '{}' --describe-only",
                file=sys.stderr,
            )
            return 1
        if status == 404:
            print(
                "\n404 means the path is wrong, not that there is no data.\n"
                f"OPENAI_COMPLIANCE_LOG_PATH is currently {path!r}. Check it against\n"
                "the documentation before concluding anything about coverage.",
                file=sys.stderr,
            )
            return 1
        if status >= 400:
            snippet = body[:400].decode("utf-8", "replace")
            print(f"\nUpstream error {status}: {snippet}", file=sys.stderr)
            return 1
        if document is None:
            print("Response was not JSON; saved verbatim for inspection.", file=sys.stderr)
            if saving:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"page-{page_number:03d}.raw").write_bytes(body)
            return 1

        items = dotted(document, items_path)
        count = len(items) if isinstance(items, list) else 0
        total_items += count
        print(f"items at {items_path!r}: {count}")

        print("shape:")
        for line in describe(document)[:60]:
            print(f"  {line}")

        if isinstance(items, list) and items:
            first_keys = sorted(items[0]) if isinstance(items[0], dict) else []
            hits = sorted(CONTENT_HINTS.intersection(first_keys))
            print()
            print(f"item keys: {first_keys}")
            if hits:
                print(f"CONTENT-LIKE FIELDS PRESENT: {hits}")
                print("  -> the feed appears to carry conversation content.")
            else:
                print("no obviously content-bearing field in the item keys.")
                print("  -> the feed may be metadata only; content may need a second")
                print("     request (see OPENAI_COMPLIANCE_FILES_PATH in the docs).")

        if saving:
            target = out_dir / f"page-{page_number:03d}.json"
            target.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
            target.chmod(0o600)
            print(f"\nsaved {target}")

        has_more = dotted(document, has_more_path)
        cursor = dotted(document, cursor_path)
        print()
        if not cursor or has_more is False:
            break

    print(f"total items across pages: {total_items}")
    if saving:
        print(f"\nThese files contain real conversation records for your workspace.")
        print(f"{out_dir} is gitignored. Delete it when you are finished:")
        print(f"  rm -rf {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
