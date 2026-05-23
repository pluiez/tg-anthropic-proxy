#!/usr/bin/env python3
"""Sync a local .env file with a template without exposing secret values.

Usage:
    python scripts/sync_env.py <from> <to>

The first argument is the env file that already contains local values, for
example `.env`. The second argument is the template that defines the desired
order, comments, and defaults, for example `.env.example`.

The script rewrites <from> in place using <to> as the template, preserving values
from <from> whenever the same key exists there. It never writes values into
<to>, so running `python scripts/sync_env.py .env .env.example` will not put
secrets into `.env.example`.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


KEY_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"


@dataclass(frozen=True)
class EnvAssignment:
    key: str
    prefix: str
    separator: str
    value: str
    newline: str
    body: str


def split_newline(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def parse_assignment(line: str) -> EnvAssignment | None:
    body, newline = split_newline(line)
    stripped = body.lstrip()
    leading_len = len(body) - len(stripped)
    leading = body[:leading_len]

    export_prefix = ""
    rest = stripped
    if rest.startswith("export "):
        export_prefix = "export "
        rest = rest[len("export "):]

    key_len = 0
    while key_len < len(rest) and rest[key_len] in KEY_CHARS:
        key_len += 1
    if key_len == 0:
        return None

    key = rest[:key_len]
    if key[0].isdigit():
        return None

    after_key = rest[key_len:]
    equals_index = after_key.find("=")
    if equals_index < 0:
        return None

    before_equals = after_key[:equals_index]
    if before_equals.strip():
        return None

    value = after_key[equals_index + 1:]
    separator = before_equals + "="
    return EnvAssignment(
        key=key,
        prefix=leading + export_prefix,
        separator=separator,
        value=value,
        newline=newline,
        body=body,
    )


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def collect_values(lines: list[str]) -> tuple[dict[str, EnvAssignment], list[str]]:
    values: dict[str, EnvAssignment] = {}
    order: list[str] = []
    for line in lines:
        assignment = parse_assignment(line)
        if assignment is None:
            continue
        if assignment.key not in values:
            order.append(assignment.key)
        values[assignment.key] = assignment
    return values, order


def render_assignment(template: EnvAssignment, value: str) -> str:
    return f"{template.prefix}{template.key}{template.separator}{value}{template.newline}"


def merge_env_lines(from_lines: list[str], template_lines: list[str]) -> tuple[list[str], dict[str, int]]:
    source_values, source_order = collect_values(from_lines)
    used_keys: set[str] = set()
    output: list[str] = []
    replaced = 0
    kept_defaults = 0

    for line in template_lines:
        template_assignment = parse_assignment(line)
        if template_assignment is None:
            output.append(line)
            continue

        source_assignment = source_values.get(template_assignment.key)
        if source_assignment is None:
            output.append(line)
            kept_defaults += 1
            continue

        output.append(render_assignment(template_assignment, source_assignment.value))
        used_keys.add(template_assignment.key)
        replaced += 1

    extra_keys = [key for key in source_order if key not in used_keys]
    if extra_keys:
        if output and not output[-1].endswith(("\n", "\r\n")):
            output[-1] += "\n"
        if output and output[-1].strip():
            output.append("\n")
        output.append("# Extra variables preserved from the source env because they are not in the template.\n")
        for key in extra_keys:
            assignment = source_values[key]
            output.append(f"{assignment.key}={assignment.value}\n")

    return output, {
        "replaced": replaced,
        "kept_defaults": kept_defaults,
        "extra_preserved": len(extra_keys),
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        finally:
            raise


def sync_env_file(from_path: Path, template_path: Path) -> dict[str, int]:
    from_lines = read_lines(from_path)
    template_lines = read_lines(template_path)
    if not template_lines:
        raise ValueError(f"template file is empty or missing: {template_path}")

    merged_lines, stats = merge_env_lines(from_lines, template_lines)
    atomic_write(from_path, "".join(merged_lines))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite <from> using <to> as an env template, preserving same-key "
            "values from <from>. The template file is read-only."
        )
    )
    parser.add_argument("from_file", help="env file containing local values; this file is updated in place")
    parser.add_argument("to_file", help="template env file defining comments, order, and defaults; read-only")
    args = parser.parse_args()

    from_path = Path(args.from_file)
    template_path = Path(args.to_file)
    stats = sync_env_file(from_path, template_path)
    print(
        "updated {from_file} from template {to_file}: "
        "preserved={replaced} defaults={kept_defaults} extras={extra_preserved}".format(
            from_file=from_path,
            to_file=template_path,
            **stats,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
