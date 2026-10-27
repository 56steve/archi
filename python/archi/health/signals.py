"""The signals the instance-health automation reads, and the problems they yield.

    doctor             `archi doctor --json`: every check that fails (a warning only if asked), including
                       source-freshness, which yields one problem per late source
    ingest             `okg sync frontier --json`: a source whose newest run failed or whose frontier is blocked
    automation-errors  the ops records (`archi.ops.shadow`'s reader seam): an automation whose recent
                       decisions are mostly errors
    eval               the `report.json` files `archi eval` writes, under `<data root>/eval/` (or the
                       configured `eval.reports_dir`): per bundle, the latest run against the bundle's
                       threshold and against the earlier runs. With no report, the signal is skipped, and
                       the skip is reported.

Each collector turns plain data into `Problem`s and reads nothing itself; `collect` runs the readers.
A reader that fails is not hidden: it becomes the problem `monitor/<signal>` ("could not read it"), and
that signal's own issues are left alone for the run (reading nothing is not evidence of health).
Every reader is a parameter, so tests give it data and the lab gives it the real thing.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from archi.health.config import HealthConfig
from archi.health.problems import Problem, make_key

#: ops decision verdicts that mean the run failed (not a cap or a person's "no")
ERROR_VERDICTS = frozenset({"error", "invalid_proposal", "refused"})
#: the file `archi eval` writes per run, and the schema it carries (`archi.evaluation.runner.REPORT_SCHEMA`)
EVAL_REPORT = "report.json"
EVAL_SCHEMA_FAMILY = "archi-eval-report/"
EVAL_SCHEMAS = frozenset({"archi-eval-report/1"})
EVAL_STATUSES = frozenset({"pass", "fail", "execution_failed"})


# --- doctor ---------------------------------------------------------------------


def doctor_problems(report: Mapping[str, Any], *, ignore: Iterable[str] = (), include_warnings: bool = False
                    ) -> list[Problem]:
    """A problem per failing check in an `archi doctor --json` report.

    `source-freshness` is split by source (`source-freshness/<source>`), from its `data.sources` rows, so each
    stale source has its own issue. Any other check is `<check id>/instance`.
    """
    skip = set(ignore)
    wanted = {"fail", "warn"} if include_warnings else {"fail"}
    out: list[Problem] = []
    for check in report.get("checks") or []:
        check_id, status = check.get("id"), check.get("status")
        if not check_id or check_id in skip or status not in wanted:
            continue
        severity = "error" if status == "fail" else "warning"
        rows = ((check.get("data") or {}).get("sources") if check_id == "source-freshness" else None)
        if rows:
            for row in rows:
                if row.get("ok"):
                    continue
                out.append(_late_source(check_id, row, severity))
            continue
        out.append(Problem(
            key=make_key(check_id, "instance"), family="doctor", severity=severity,
            title=f"archi doctor: {check_id} {'failed' if status == 'fail' else 'warns'}",
            detail=_doctor_detail(check),
        ))
    return out


def _late_source(check_id: str, row: Mapping[str, Any], severity: str) -> Problem:
    source = row.get("source") or "unknown"
    last = row.get("last_reconcile_at")
    # no age here: it changes every hour, and the issue is rewritten whenever its text changes
    lines = [
        f"Source `{source}` is over its freshness limit (`max_staleness: {row.get('max_staleness')}`).",
        "",
        f"- Last completed reconcile: {last or 'none recorded'}",
    ]
    if not last and row.get("reason"):
        lines.append(f"- {row['reason']}")
    lines += ["", "Fix: `okg ingest --deployment <instance name>` (run in the instance directory), or relax the "
              "limit in the pack's `freshness:` and re-render. Source: `archi doctor --json`, check "
              f"`{check_id}`."]
    return Problem(make_key(check_id, source), "doctor", f"Source {source} is stale", "\n".join(lines), severity)


def _doctor_detail(check: Mapping[str, Any]) -> str:
    lines = [f"`archi doctor` check `{check['id']}` is {check['status']}.", "", f"> {check.get('message', '')}"]
    if check.get("remediation"):
        lines += ["", f"Fix: {check['remediation']}"]
    if check.get("footgun"):
        lines += ["", f"Footgun ledger: {check['footgun']} (docs/docs/operating/footguns.md)."]
    return "\n".join(lines)


# --- ingest ---------------------------------------------------------------------


def frontier_problems(report: Mapping[str, Any]) -> list[Problem]:
    """A problem per source whose frontier is `blocked` in `okg sync frontier --json`."""
    out = []
    for row in report.get("rows") or []:
        if row.get("frontier_state") != "blocked":
            continue
        source = row.get("registry_source_name") or row.get("source_name") or "unknown"
        lines = [f"okg reports source `{source}` blocked: it cannot move its data into the published graph.", ""]
        for label, field_name in (("Read visibility", "read_visibility_state"), ("Last attempt", "last_attempt_status"),
                                  ("Last attempt run", "last_attempt_run_id"), ("Last error", "last_error")):
            if row.get(field_name):
                lines.append(f"- {label}: {str(row[field_name])[:400]}")
        lines += ["", f"Look: `okg sync frontier --deployment <instance name> --source {source} --json`, then fix "
                  "the cause and re-run the ingest."]
        out.append(Problem(make_key("ingest", source), "ingest", f"Ingest of {source} is blocked", "\n".join(lines)))
    return out


# --- automation error rates -------------------------------------------------------


def automation_error_problems(
    source: Callable[[str], list],
    now: datetime,
    *,
    window: timedelta = timedelta(hours=24),
    min_decisions: int = 3,
    max_rate: float = 0.5,
) -> list[Problem]:
    """A problem per automation whose decisions in the window are mostly errors.

    `source(subtype)` is the reader seam of `archi.ops.shadow` (rows of `{"node_id", "attrs"}`). Placeholder
    decisions and decisions with no usable time are not counted. Fewer than `min_decisions` in the window say
    too little to call a rate.
    """
    from archi.ops.shadow import parse_time

    counts: dict[str, list[int]] = {}
    for row in source("ops_decision"):
        attrs = row.get("attrs") or {}
        name, moment = attrs.get("automation"), parse_time(attrs.get("decided_at"))
        if not name or moment is None or not now - window <= moment <= now:
            continue
        tally = counts.setdefault(name, [0, 0])
        tally[0] += 1
        tally[1] += attrs.get("verdict") in ERROR_VERDICTS
    out = []
    hours = window.total_seconds() / 3600
    for name in sorted(counts):
        total, errors = counts[name]
        if total < min_decisions or errors / total < max_rate:
            continue
        out.append(Problem(
            key=make_key("automation-errors", name), family="automation-errors",
            title=f"Automation {name} is failing",
            detail=(f"{errors} of the last {total} decisions of automation `{name}` in the past {hours:g} hours "
                    f"were errors (threshold: {max_rate:.0%} of at least {min_decisions}).\n\n"
                    f"Look: `archi ops status`, and the `ops_decision` records with `automation: {name}` and a "
                    "verdict of `error`, `invalid_proposal` or `refused`."),
        ))
    return out


# --- eval -----------------------------------------------------------------------


def eval_reports_dir(instance_dir: Path | str, configured: Optional[str] = None) -> Path:
    """Where the instance's eval reports are.

    `archi eval` writes `report.json` to `<data root>/eval/<bundle>/` by default (`--out` moves it), so the
    default is `<data root>/eval/`, the data root read from the instance's `archi.yaml`. `configured` (the
    `eval.reports_dir` setting) replaces it; a relative path is taken from the instance directory.
    """
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else Path(instance_dir) / path
    from archi.stack.instance import Instance

    return Instance.load(Path(instance_dir)).data_root / "eval"


@dataclass(frozen=True)
class EvalRun:
    """One `archi eval` run of one bundle, as its `report.json` records it."""

    bundle: str
    model: str
    started_at: str
    status: str
    passed: int
    total: int
    threshold: int
    error: Optional[str]
    path: str  # relative to the reports directory

    @property
    def score(self) -> float:
        return self.passed / self.total


def read_eval_runs(directory: Path) -> list[EvalRun]:
    """Every `report.json` under `directory` (any depth) that `archi eval` wrote, oldest first.

    A `report.json` with no `archi-eval-report/...` schema is another kind of report and is left out. One that
    claims the schema but cannot be read, or is a version this reader does not know, raises: a report that
    cannot be read is not evidence of health.
    """
    runs: dict[tuple[str, str, str], EvalRun] = {}
    for path in sorted(directory.rglob(EVAL_REPORT)) if directory.is_dir() else []:
        rel = path.relative_to(directory).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{rel} under {directory}: not readable JSON ({type(exc).__name__}: {exc})") from exc
        schema = data.get("schema") if isinstance(data, dict) else None
        if not isinstance(schema, str) or not schema.startswith(EVAL_SCHEMA_FAMILY):
            continue
        if schema not in EVAL_SCHEMAS:
            raise ValueError(f"{rel} under {directory}: eval report schema {schema!r} is not one this reader "
                             f"knows ({sorted(EVAL_SCHEMAS)})")
        try:
            run = EvalRun(
                bundle=_text(data, "bundle"), model=_text(data, "model"), started_at=_text(data, "started_at"),
                status=_text(data, "status"), passed=_count(data, "passed"), total=_count(data, "total"),
                threshold=_count(data, "pass_threshold"), error=data.get("error") or None, path=rel,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{rel} under {directory}: not a usable eval report ({exc})") from exc
        if run.status not in EVAL_STATUSES:
            raise ValueError(f"{rel} under {directory}: status {run.status!r} is not one of {sorted(EVAL_STATUSES)}")
        if run.status != "execution_failed" and run.total == 0:
            raise ValueError(f"{rel} under {directory}: status {run.status} with no questions")
        runs.setdefault((run.bundle, run.started_at, run.model), run)  # the same run copied twice counts once
    return sorted(runs.values(), key=lambda r: (r.started_at, r.path))


def _text(data: Mapping[str, Any], name: str) -> str:
    value = data[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _count(data: Mapping[str, Any], name: str) -> int:
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a count")
    return value


def eval_problems(directory: Path | str, *, min_drop: float = 0.1, history: int = 3
                  ) -> tuple[list[Problem], list[dict[str, str]]]:
    """(problems, skip notes) from the `archi eval` reports under `directory`, one problem per bundle at most.

    For each bundle, the latest run is a problem (`eval/<bundle>`) when:

    - it ended `execution_failed`: the run could not ask the instance, so this is a failed health signal and
      no score is judged (an error);
    - it passed fewer questions than the bundle's threshold (an error);
    - its score (passed / total) is more than `min_drop` below the mean score of up to `history` earlier
      runs of the same bundle and the same model that did execute (a warning while it still meets the
      threshold). A different model is a different measurement, so it is not compared.

    Skip notes are `{"signal", "reason"}`. `eval`: there is no report at all, so the signal is not judged.
    `eval-trend`: a bundle has no earlier run to compare with; its threshold is still judged.
    """
    directory = Path(directory)
    runs = read_eval_runs(directory)
    if not runs:
        return [], [{"signal": "eval", "reason": f"no eval report exists under {directory} (run `archi eval`); "
                                                 "eval trends are skipped"}]
    by_bundle: dict[str, list[EvalRun]] = {}
    for run in runs:
        by_bundle.setdefault(run.bundle, []).append(run)
    problems: list[Problem] = []
    notes: list[dict[str, str]] = []
    for bundle in sorted(by_bundle):
        *before, latest = by_bundle[bundle]
        head = (f"The latest `archi eval` run of bundle `{bundle}` (model `{latest.model}`, started "
                f"{latest.started_at})")
        look = (f"Look: `{latest.path}` under `{directory}` (and `report.md` beside it); re-run with "
                f"`archi eval --bundle {bundle}`.")
        if latest.status == "execution_failed":
            problems.append(Problem(
                key=make_key("eval", bundle), family="eval", severity="error",
                title=f"Eval of {bundle} could not run",
                detail=(f"{head} ended `execution_failed`: {latest.error or 'no question could be asked'}.\n\n"
                        "This is a failed health signal, not a low score: the run could not ask the instance "
                        "(MCP endpoint unreachable or offering no tools, or the model failing), so no score was "
                        "compared with the threshold or with earlier runs.\n\n"
                        f"{look} Check `archi doctor`, the MCP endpoint and the model server first."),
            ))
            continue
        earlier = [r for r in before if r.status != "execution_failed" and r.model == latest.model][-history:]
        below = latest.passed < latest.threshold
        drop = None
        if earlier:
            baseline = statistics.fmean(r.score for r in earlier)
            # rounded: 0.8 - 0.7 is 0.10000000000000009 in floating point, which is not "more than 0.1"
            drop = (baseline, round(baseline - latest.score, 9))
        else:
            notes.append({"signal": "eval-trend", "reason": (
                f"bundle {bundle}: no earlier run of model {latest.model} to compare with (archi eval overwrites "
                f"its report; keep runs apart with `--out <dir>/{bundle}/<date>` under {directory})")})
        dropped = drop is not None and drop[1] > min_drop
        if not (below or dropped):
            continue
        lines = [f"{head} passed {latest.passed} of {latest.total} questions (score {latest.score:.2f}).", ""]
        if below:
            lines.append(f"- That is below the bundle's threshold of {latest.threshold}.")
        if dropped:
            lines.append(f"- That is {drop[1]:.2f} below the mean score {drop[0]:.2f} of the {len(earlier)} earlier "
                         f"run(s) of the same model; the allowed drop is {min_drop:g} (`eval.min_drop`).")
        lines += ["", look]
        title = f"Eval of {bundle} is below its threshold" if below else f"Eval score of {bundle} dropped"
        problems.append(Problem(key=make_key("eval", bundle), family="eval", severity="error" if below else "warning",
                                title=title, detail="\n".join(lines)))
    return problems, notes


# --- readers (the real ones) -------------------------------------------------------


def run_doctor(instance_dir: Path | str, *, timeout: int = 300) -> dict[str, Any]:
    """`archi doctor --json` for the instance. Exit 1 (a failed check) is a report; any other failure raises."""
    code = "import sys; from archi.bundles.cli import main; sys.exit(main(sys.argv[1:]))"
    done = subprocess.run([sys.executable, "-c", code, "doctor", "--instance", str(instance_dir), "--json"],
                          capture_output=True, text=True, timeout=timeout)
    try:
        report = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(f"archi doctor --json gave no JSON (exit {done.returncode}): "
                           f"{(done.stderr or done.stdout)[-500:].strip()}") from None
    if done.returncode not in (0, 1) or report.get("error"):
        raise RuntimeError(f"archi doctor could not run (exit {done.returncode}): {report.get('error') or done.stderr[-300:]}")
    return report


def run_frontier(deployment: str, dsn: str, *, timeout: int = 120) -> dict[str, Any]:
    """`okg sync frontier --json`: per-source frontier rows (okg 0.2.0)."""
    from archi.ops.record import _okg

    done = subprocess.run([_okg(), "sync", "frontier", "--deployment", deployment, "--dsn", dsn, "--json"],
                          capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(f"okg sync frontier gave no JSON (exit {done.returncode}): "
                           f"{(done.stderr or done.stdout)[-500:].strip()}") from None
    report = data.get("data") if isinstance(data.get("data"), dict) else data
    if done.returncode != 0 or "rows" not in report:
        raise RuntimeError(f"okg sync frontier failed (exit {done.returncode}): "
                           f"{json.dumps(data)[:400]}")
    return report


# --- the whole observation ------------------------------------------------------------


@dataclass
class Observation:
    problems: list[Problem] = field(default_factory=list)
    #: the families read successfully in this run: only their issues may be closed
    scope: list[str] = field(default_factory=list)
    #: `{"signal", "reason"}` for each signal not applicable: `eval` (no eval report; not judged) or
    #: `eval-trend` (a bundle with no earlier run to compare; its threshold was still judged)
    skipped: list[dict[str, str]] = field(default_factory=list)


def collect(
    config: HealthConfig,
    *,
    instance_dir: Path | str,
    now: datetime,
    read_doctor: Callable[[], Mapping[str, Any]],
    read_frontier: Callable[[], Mapping[str, Any]],
    read_ops_records: Callable[[str], list],
) -> Observation:
    """Run the readers and gather the problems. A reader that raises becomes `monitor/<signal>`."""
    obs = Observation()
    failed: dict[str, str] = {}

    def attempt(signal: str, family: str, work: Callable[[], list[Problem]]) -> None:
        try:
            obs.problems += work()
            obs.scope.append(family)
        except Exception as exc:  # noqa: BLE001 - reported as a problem, not swallowed
            failed[signal] = f"{type(exc).__name__}: {exc}"[:500]

    attempt("doctor", "doctor", lambda: doctor_problems(
        read_doctor(), ignore=config.ignore_checks, include_warnings=config.include_warnings))
    attempt("ingest", "ingest", lambda: frontier_problems(read_frontier()))
    attempt("automation-errors", "automation-errors", lambda: automation_error_problems(
        read_ops_records, now, window=timedelta(hours=config.window_hours),
        min_decisions=config.min_decisions, max_rate=config.max_error_rate))

    def eval_signal() -> list[Problem]:
        found, notes = eval_problems(eval_reports_dir(instance_dir, config.eval_reports_dir),
                                     min_drop=config.eval_min_drop)
        # `eval` skipped: nothing to judge, and its issues (if any) are not closed on that account.
        # `eval-trend` only says a bundle has no earlier run; its threshold was judged.
        obs.skipped.extend(notes)
        return found

    attempt("eval", "eval", eval_signal)
    if any(s["signal"] == "eval" for s in obs.skipped) and "eval" in obs.scope:
        obs.scope.remove("eval")
    for signal, reason in sorted(failed.items()):
        obs.problems.append(Problem(
            key=make_key("monitor", signal), family="monitor", severity="error",
            title=f"instance-health could not read {signal}",
            detail=(f"The instance-health automation could not read the `{signal}` signal, so it cannot say "
                    f"whether that part of the instance is healthy.\n\n> {reason}\n\nIts existing issues are "
                    "left as they are until it can read the signal again."),
        ))
    obs.scope.append("monitor")
    obs.problems.sort(key=lambda p: p.key)
    return obs
