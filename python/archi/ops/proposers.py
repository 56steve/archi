"""Proposers: the agent step of the loop (ADR 0002 D11, agents propose).

A proposer reads the pinned context and returns a raw proposal mapping:
`summary`, `evidence_ids` (okg node ids it cites), and optionally
`action_class`, `payload`, `confidence`, `rollback`, and `draft` (L1). The loop
validates the mapping; a proposer holds no execution credential.

`StubProposer` returns fixed answers from a fixture, for tests and scratch
runs. The model-backed proposer comes later.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class ProposeRequest:
    automation: str
    signal: Mapping[str, Any]
    context: Mapping[str, Any]
    generation: str
    #: 0, 1 or 2
    level: int


@runtime_checkable
class Proposer(Protocol):
    def propose(self, request: ProposeRequest) -> Mapping[str, Any]: ...


class ProposerError(RuntimeError):
    """The proposer could not produce a proposal."""


@dataclass
class StubProposer:
    """Deterministic proposer.

    fixture = {
        "by_signal": {"sig-1": {...proposal...}},   # looked up by signal id
        "default": {...proposal...},                # used when no signal matches
        "fail": {"sig-2": "model timeout"},         # raise ProposerError for these
    }
    """

    fixture: Mapping[str, Any]
    calls: list[ProposeRequest] = field(default_factory=list)

    def propose(self, request: ProposeRequest) -> Mapping[str, Any]:
        self.calls.append(request)
        signal_id = request.signal.get("id")
        failures = self.fixture.get("fail") or {}
        if signal_id in failures:
            raise ProposerError(failures[signal_id])
        by_signal = self.fixture.get("by_signal") or {}
        if signal_id in by_signal:
            return copy.deepcopy(by_signal[signal_id])
        if "default" in self.fixture:
            return copy.deepcopy(self.fixture["default"])
        raise ProposerError(f"fixture has no proposal for signal {signal_id!r}")
