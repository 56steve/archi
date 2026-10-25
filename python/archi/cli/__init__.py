"""The instance lifecycle commands: `archi init`, `up`, `down`, `doctor`.

`archi.bundles.cli` owns the parser; `register` adds these commands to it.
"""
from __future__ import annotations

import argparse


def register(sub, roots) -> None:
    from archi.cli import doctor, init, up

    init.add_parser(sub, roots)
    up.add_parsers(sub)
    doctor.add_parser(sub)


def finalize(args: argparse.Namespace) -> None:
    from archi.cli.init import finalize_args

    finalize_args(args)
