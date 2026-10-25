"""The `archi` command: pack and bundle tooling.

    archi bundles validate [PATH ...]   check pack.yaml / bundle.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from archi.bundles import discovery
from archi.bundles.manifest import BUNDLE_FILE, PACK_FILE, Problem, validate_bundle, validate_pack


def _manifest_dir(path: Path) -> Path:
    return path.parent if path.name in (PACK_FILE, BUNDLE_FILE) else path


def _catalog(args: argparse.Namespace) -> discovery.Catalog:
    return discovery.discover(args.pack_root, args.bundle_root)


def cmd_validate(args: argparse.Namespace) -> int:
    catalog = _catalog(args)
    problems: list[Problem] = []
    checked = 0
    if not args.paths:
        problems = list(catalog.problems)
        checked = len(catalog.packs) + len(catalog.bundles) + len(
            {p.path.parent for p in catalog.problems}
        )
    for raw in args.paths:
        directory = _manifest_dir(Path(raw))
        checked += 1
        if (directory / PACK_FILE).is_file():
            problems.extend(validate_pack(directory))
        elif (directory / BUNDLE_FILE).is_file():
            problems.extend(validate_bundle(directory, catalog.packs, catalog.bundles))
        else:
            problems.append(Problem(directory, f"holds neither {PACK_FILE} nor {BUNDLE_FILE}"))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"ok: {checked} manifest(s) valid")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="archi", description="Archi pack and bundle tooling.")
    sub = parser.add_subparsers(dest="command", required=True)

    def roots(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--pack-root", action="append", type=Path, default=[],
            help="extra directory of <name>/pack.yaml entries (repeatable)",
        )
        p.add_argument(
            "--bundle-root", action="append", type=Path, default=[],
            help="extra directory of <name>/bundle.yaml entries (repeatable)",
        )

    bundles = sub.add_parser("bundles", help="inspect and check packs and bundles")
    bsub = bundles.add_subparsers(dest="bundles_command", required=True)
    validate = bsub.add_parser(
        "validate",
        help="check manifests against the published schemas and the composition rules",
    )
    validate.add_argument(
        "paths", nargs="*",
        help="pack or bundle directories (or their manifest files); default: every one found",
    )
    roots(validate)
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
