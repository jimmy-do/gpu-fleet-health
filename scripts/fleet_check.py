#!/usr/bin/env python3
"""fleet_check.py: decide which GPU nodes to cordon/drain from a health report.

The report is JSON, read from a file path or an http(s) URL:

    {"nodes": [{"name": "gpu-node-01", "gpus_expected": 8, "gpus_found": 8,
                "max_temp_c": 64, "xid_errors": 0}, ...]}

Safety rails:
  * Dry run by default: prints the kubectl commands. --execute runs them.
  * Incomplete records are skipped with a reason, never guessed at.
  * Refuses to act if more than --max-unhealthy-pct (default 10%) of evaluated
    nodes look unhealthy. That pattern usually means a fleet-wide or
    monitoring fault, and draining half the fleet would make it worse.

Exit codes:
  0  all evaluated nodes healthy
  1  unhealthy nodes found (commands printed, or run with --execute)
  2  refused to act: too many unhealthy nodes
  3  could not read the report (connection refused, timeout, bad JSON, no usable records)
  4  a kubectl command failed (--execute only)
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

# Field -> accepted type(s). bool is rejected separately (it subclasses int).
REQUIRED_FIELDS = {
    "name": str,
    "gpus_expected": int,
    "gpus_found": int,
    "max_temp_c": (int, float),
    "xid_errors": int,
}
# Kubernetes node names are DNS-1123 subdomains. Validating them also stops a
# name like "--all" from being passed to kubectl as a flag.
NODE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


class SourceError(Exception):
    """The health report could not be fetched or parsed."""


def load_nodes(source: str, timeout: float) -> list:
    """Return the "nodes" list from a file or URL, or raise SourceError."""
    try:
        if source.startswith(("http://", "https://")):
            with urllib.request.urlopen(source, timeout=timeout) as resp:
                data = json.load(resp)
        else:
            with open(source, encoding="utf-8") as fh:
                data = json.load(fh)
    except urllib.error.URLError as exc:
        # urlopen wraps connect-time socket errors; the cause is in .reason.
        if isinstance(exc.reason, ConnectionRefusedError):
            raise SourceError(f"connection refused: {source}") from exc
        if isinstance(exc.reason, TimeoutError):
            raise SourceError(f"timed out after {timeout}s: {source}") from exc
        raise SourceError(f"cannot fetch {source}: {exc.reason}") from exc
    except TimeoutError as exc:  # connected, but the response never arrived
        raise SourceError(f"timed out after {timeout}s: {source}") from exc
    except (OSError, ValueError) as exc:  # missing file, bad JSON, reset socket
        raise SourceError(f"cannot read {source}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise SourceError(f'{source}: expected an object with a "nodes" list')
    return data["nodes"]


def incomplete_reason(record) -> str | None:
    """Why a record can't be evaluated, or None if it is complete."""
    if not isinstance(record, dict):
        return "record is not an object"
    for field, kind in REQUIRED_FIELDS.items():
        value = record.get(field)
        if value is None:
            return f"missing '{field}'"
        if isinstance(value, bool) or not isinstance(value, kind):
            return f"'{field}' has wrong type ({type(value).__name__})"
    if not NODE_NAME_RE.match(record["name"]):
        return f"invalid node name {record['name']!r}"
    return None


def node_problems(node: dict, max_temp_c: float) -> list[str]:
    """Health problems for one complete record; empty list means healthy."""
    problems = []
    if node["gpus_found"] < node["gpus_expected"]:
        problems.append(f"GPU missing ({node['gpus_found']}/{node['gpus_expected']})")
    if node["max_temp_c"] > max_temp_c:
        problems.append(f"temperature {node['max_temp_c']}C > {max_temp_c}C")
    if node["xid_errors"] > 0:
        problems.append(f"{node['xid_errors']} XID error(s)")
    return problems


def remediation(name: str) -> list[list[str]]:
    """Commands that take a node out of service, in order."""
    return [
        ["kubectl", "cordon", name],
        ["kubectl", "drain", name, "--ignore-daemonsets",
         "--delete-emptydir-data", "--timeout=10m"],
    ]


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("source", help="path or http(s) URL of the node health JSON")
    p.add_argument("--execute", action="store_true",
                   help="actually run kubectl (default: dry run, print only)")
    p.add_argument("--max-temp", type=float, default=83.0,
                   help="unhealthy above this temperature in C (default: 83)")
    p.add_argument("--max-unhealthy-pct", type=float, default=10.0,
                   help="refuse to act above this %% of unhealthy nodes (default: 10)")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="HTTP timeout in seconds (default: 5)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        records = load_nodes(args.source, args.timeout)
    except SourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    # Classify every record: skipped, healthy, or unhealthy.
    unhealthy: dict[str, list[str]] = {}
    evaluated = 0
    for i, record in enumerate(records):
        reason = incomplete_reason(record)
        if reason:
            label = record.get("name") if isinstance(record, dict) else None
            print(f"SKIP {label if isinstance(label, str) else f'record[{i}]'}: {reason}")
            continue
        evaluated += 1
        problems = node_problems(record, args.max_temp)
        if problems:
            unhealthy[record["name"]] = problems
            print(f"UNHEALTHY {record['name']}: {'; '.join(problems)}")
        else:
            print(f"OK {record['name']}")

    if evaluated == 0:
        print("ERROR: no complete records to evaluate", file=sys.stderr)
        return 3
    print(f"SUMMARY: {evaluated} evaluated, {len(unhealthy)} unhealthy, "
          f"{len(records) - evaluated} skipped")
    if not unhealthy:
        return 0

    # Blast-radius guard: computed over evaluated nodes, so skipped records
    # can't dilute the ratio and make a mass drain look acceptable.
    pct = 100.0 * len(unhealthy) / evaluated
    if pct > args.max_unhealthy_pct:
        print(f"REFUSING TO ACT: {len(unhealthy)}/{evaluated} nodes ({pct:.0f}%) "
              f"unhealthy exceeds the {args.max_unhealthy_pct:g}% limit. This looks "
              "like a fleet-wide or monitoring problem; investigate before draining.")
        return 2

    for name in unhealthy:
        for cmd in remediation(name):
            if not args.execute:
                print(f"DRY RUN: would run: {shlex.join(cmd)}")
                continue
            print(f"RUN: {shlex.join(cmd)}")
            try:
                subprocess.run(cmd, check=True, timeout=900)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                print(f"ERROR: {shlex.join(cmd)} failed: {exc}", file=sys.stderr)
                return 4
    return 1


if __name__ == "__main__":
    sys.exit(main())
