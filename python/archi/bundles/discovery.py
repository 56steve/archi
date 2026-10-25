"""Find packs and bundles.

A *pack root* is a directory holding `<name>/pack.yaml` entries; a *bundle
root* holds `<name>/bundle.yaml` entries. archi's own roots are
`bundles/packs/` and `bundles/` (in the wheel, under `archi/_payload/`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from archi.bundles.manifest import (
    BUNDLE_FILE,
    PACK_FILE,
    Bundle,
    Pack,
    Problem,
    load_pack,
    ManifestError,
    read_bundle,
    validate_bundle,
)


@dataclass
class Catalog:
    """Every pack and bundle found, plus the problems found on the way.

    A pack or bundle that fails to load is left out of `packs`/`bundles` and
    reported in `problems`, so one broken manifest does not hide the rest.
    """

    packs: dict[str, Pack] = field(default_factory=dict)
    bundles: dict[str, Bundle] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    def add_pack_dir(self, directory: Path) -> None:
        try:
            pack = load_pack(directory)
        except ManifestError as exc:
            self.problems.extend(exc.problems)
            return
        other = self.packs.get(pack.name)
        if other is not None and other.dir.resolve() != pack.dir.resolve():
            self.problems.append(
                Problem(pack.dir / PACK_FILE, f"pack {pack.name!r} is also provided by {other.dir}")
            )
            return
        self.packs[pack.name] = pack

    def add_bundle_dir(self, directory: Path) -> None:
        bundle, problems = read_bundle(directory)
        self.problems.extend(problems)
        if bundle is None or problems:
            return
        other = self.bundles.get(bundle.name)
        if other is not None and other.dir.resolve() != bundle.dir.resolve():
            self.problems.append(
                Problem(bundle.dir / BUNDLE_FILE, f"bundle {bundle.name!r} is also provided by {other.dir}")
            )
            return
        self.bundles[bundle.name] = bundle

    def check_bundles(self) -> None:
        """Check each bundle's references now that every pack is known."""
        failed = []
        for name in sorted(self.bundles):
            problems = validate_bundle(self.bundles[name].dir, self.packs, self.bundles)
            if problems:
                self.problems.extend(problems)
                failed.append(name)
        for name in failed:
            del self.bundles[name]


def _children(root: Path, manifest: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / manifest).is_file())


def from_roots(
    pack_roots: Iterable[Path] = (),
    bundle_roots: Iterable[Path] = (),
) -> Catalog:
    catalog = Catalog()
    for root in pack_roots:
        for directory in _children(Path(root), PACK_FILE):
            catalog.add_pack_dir(directory)
    for root in bundle_roots:
        for directory in _children(Path(root), BUNDLE_FILE):
            catalog.add_bundle_dir(directory)
    catalog.check_bundles()
    return catalog


def default_roots() -> tuple[list[Path], list[Path]]:
    """archi's own pack and bundle roots."""
    from archi import paths

    return [paths.packs_dir()], [paths.profiles_dir()]


def discover(
    extra_pack_roots: Iterable[Path] = (),
    extra_bundle_roots: Iterable[Path] = (),
) -> Catalog:
    packs, bundles = default_roots()
    return from_roots([*packs, *extra_pack_roots], [*bundles, *extra_bundle_roots])
