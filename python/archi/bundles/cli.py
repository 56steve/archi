"""The `archi` command: pack and bundle tooling.

    archi bundles validate [PATH ...]   check pack.yaml / bundle.yaml
    archi render --bundle B [--with P] [--without P] [--instance DIR]
                                        compose packs into an okg profile
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


def _packs_arg(values: list[str]) -> list[str]:
    """--with a,b --with c -> [a, b, c]"""
    return [name for value in values for name in value.split(",") if name]


def cmd_render(args: argparse.Namespace) -> int:
    from archi.bundles.render import render
    from archi.bundles.resolve import CompositionError, resolve

    catalog = _catalog(args)
    for problem in catalog.problems:
        print(f"warning: {problem}", file=sys.stderr)
    try:
        resolution = resolve(
            catalog, args.bundle, _packs_arg(args.with_packs), _packs_arg(args.without_packs)
        )
        rendered = render(resolution, args.instance, name=args.name)
    except CompositionError as exc:
        print(f"error ({exc.code}): {exc}", file=sys.stderr)
        return 1
    for note in resolution.notes:
        print(f"note: {note}", file=sys.stderr)
    print(f"rendered profile {rendered.name!r} with packs {', '.join(resolution.pack_names)}")
    print(f"  {rendered.profile_dir}")
    print(f"run okg from {args.instance} so it finds profiles/{rendered.name}/:")
    print(f"  okg install --profile {rendered.name} --deployment-name <slug> --postgres-dsn ...")
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

    render = sub.add_parser("render", help="compose bundles and packs into one okg profile")
    render.add_argument("--bundle", action="append", required=True,
                        help="bundle to install (repeatable; chat scalars come from the last)")
    render.add_argument("--with", dest="with_packs", action="append", default=[],
                        help="add packs (comma-separated or repeated)")
    render.add_argument("--without", dest="without_packs", action="append", default=[],
                        help="remove packs (comma-separated or repeated)")
    render.add_argument("--instance", type=Path, default=Path("."),
                        help="instance directory; the profile goes to <instance>/profiles/<name>/")
    render.add_argument("--name", help="profile name (default: the bundle names joined by -)")
    roots(render)
    render.set_defaults(func=cmd_render)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
