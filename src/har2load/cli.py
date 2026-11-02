"""Dependency-free CLI. JSON on stdout; opt-in, value-free logs on stderr."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Never

from har2load import __version__
from har2load.application import Application, error_response
from har2load.domain import Har2LoadError, JSONValue


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse messages can echo paths, credentials and arbitrary input tokens.
        raise Har2LoadError("CLI_ARGUMENT_ERROR", "Invalid arguments; consult --help.")


def parser() -> argparse.ArgumentParser:
    root = SafeArgumentParser(
        description="Offline HAR import and bounded inspection (early alpha)."
    )
    root.add_argument("--version", action="version", version=f"Har2Load {__version__}")
    root.add_argument("--verbose", action="store_true", help="Value-free progress logs to stderr")
    commands = root.add_subparsers(dest="operation", required=True)
    importing = commands.add_parser(
        "import", help="Create a new private workspace; never overwrite"
    )
    importing.add_argument("source", type=Path, help="UTF-8 HAR file (up to 64 MiB)")
    importing.add_argument("--workspace", required=True, type=Path, help="New directory")
    importing.add_argument("--step-gap-seconds", type=float, default=5.0)
    summary = commands.add_parser("summary", help="Page through entry metadata (no URLs or bodies)")
    summary.add_argument("--workspace", required=True, type=Path)
    summary.add_argument("--offset", type=int, default=0)
    summary.add_argument("--limit", type=int, default=20, help="1 to 100 entries")
    summary.add_argument("--revision", type=int, help="Read an immutable historical revision")
    inspect = commands.add_parser("inspect", help="Read an explicit, unredacted bounded fragment")
    inspect.add_argument("--workspace", required=True, type=Path)
    inspect.add_argument("--exchange", help="Stable exchange ID from summary")
    inspect.add_argument("--section", default="entry", choices=[
        "entry", "request", "response", "request-body", "response-body", "groups", "diagnostics",
        "scenario", "history", "conflicts",
        "parameters", "variables", "extractors", "bindings", "extraction-checks",
        "validation-diagnostics", "analysis", "candidates", "analysis-diagnostics",
        "target-extensions", "code-blocks", "helpers", "extension-plan",
    ])
    inspect.add_argument("--pointer", default="", help="RFC 6901 pointer relative to selected JSON")
    inspect.add_argument("--offset", type=int, default=0, help="Character offset, not byte offset")
    inspect.add_argument("--limit", type=int, default=4096, help="1 to 4096 characters")
    inspect.add_argument("--revision", type=int, help="Read an immutable historical revision")
    changes = commands.add_parser(
        "changes", help="Validate/apply a typed changeset or inspect history"
    )
    actions = changes.add_subparsers(dest="action", required=True)
    for verb in ("validate", "apply"):
        action = actions.add_parser(verb)
        action.add_argument("changeset", type=Path, help="Strict JSON changeset, up to 1 MiB")
        action.add_argument("--workspace", required=True, type=Path)
    history = actions.add_parser("history", help="Page through committed change metadata")
    history.add_argument("--workspace", required=True, type=Path)
    history.add_argument("--offset", type=int, default=0)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--revision", type=int)
    validate = commands.add_parser(
        "validate", help="Check model contracts and recorded extractions"
    )
    validate.add_argument("--workspace", required=True, type=Path)
    validate.add_argument("--offset", type=int, default=0)
    validate.add_argument("--limit", type=int, default=20)
    validate.add_argument("--revision", type=int)
    analyze = commands.add_parser(
        "analyze", help="Discover bounded candidates without applying edits"
    )
    analyze.add_argument("--workspace", required=True, type=Path)
    analyze.add_argument("--offset", type=int, default=0)
    analyze.add_argument("--limit", type=int, default=20)
    analyze.add_argument("--revision", type=int)
    report = commands.add_parser(
        "report", help="Read/export a private revision-pinned evidence report"
    )
    report.add_argument("--workspace", required=True, type=Path)
    report.add_argument("--revision", type=int)
    report.add_argument("--compare-revision", type=int)
    report.add_argument("--format", choices=("json", "markdown"), default="json")
    report.add_argument("--offset", type=int, default=0)
    report.add_argument("--limit", type=int, default=4096)
    report.add_argument(
        "--output", type=Path, help="New directory for JSON/Markdown pair and manifest"
    )
    return root


def main(argv: list[str] | None = None) -> int:
    operation = "cli"
    exit_code = 0
    result: dict[str, JSONValue]
    try:
        args = parser().parse_args(argv)
        operation = args.operation
        if args.verbose:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        app = Application()
        if operation == "import":
            result = app.import_capture(args.source, args.workspace, args.step_gap_seconds)
        elif operation == "summary":
            result = app.summary(args.workspace, args.offset, args.limit, args.revision)
        elif operation == "validate":
            result = app.validate(args.workspace, args.offset, args.limit, args.revision)
            if result["status"] == "error":
                exit_code = 3
        elif operation == "analyze":
            result = app.analyze(args.workspace, args.offset, args.limit, args.revision)
        elif operation == "report":
            result = app.report(
                args.workspace, format=args.format, offset=args.offset, limit=args.limit,
                revision=args.revision, compare_revision=args.compare_revision, output=args.output,
            )
        elif operation == "changes":
            operation = f"changes.{args.action}"
            if args.action == "validate":
                result = app.changes_validate(args.workspace, args.changeset)
            elif args.action == "apply":
                result = app.changes_apply(args.workspace, args.changeset)
            else:
                result = app.changes_history(args.workspace, args.offset, args.limit, args.revision)
        else:
            result = app.inspect(
                args.workspace, args.exchange, section=args.section,
                pointer=args.pointer, offset=args.offset, limit=args.limit,
                revision=args.revision,
            )
    except Har2LoadError as error:
        result = error_response(operation, error)
        exit_code = error.exit_code
    except (OSError, ValueError, RecursionError, OverflowError):
        # Do not expose source fragments or path-bearing exception text.
        result = error_response(operation, Har2LoadError("INTERNAL_ERROR", "Operation failed.", 1))
        exit_code = 1
    output = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(output.encode("utf-8")) > 65536:
        output = json.dumps(error_response(
            operation,
            Har2LoadError("OUTPUT_LIMIT", "Output exceeds limit; select a smaller view.", 2),
        ))
        exit_code = 2
    try:
        sys.stdout.write(output + "\n")
    except BrokenPipeError:
        return 5
    return exit_code
