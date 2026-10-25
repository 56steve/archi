"""Score an automation's shadow proposals against what people did later
(PACT r3-ops-runtime, req.shadow; ADR 0002 sections 7.3 and 7.4).

In shadow mode an automation proposes but nothing runs. This module reads the
ops records of one automation over a window and says how its proposals
compare with the people who handled the same incidents. It never writes.

Reading. Through the same reader seam as `archi.ops.precedents`
(`reader(subtype, attr, value) -> rows`), so tests use a fake reader and live
use is `okg filter` pinned to the published generation. Proposals come from
`ops_proposal.automation`; per proposal the walk reads its incident, the
decisions on it, the incident's proposals and outcomes, and the actions they
name.

Scope. A proposal is scored when its `mode` is `shadow` and its `proposed_at`
lies in [since, until]. Proposals in other modes are counted as
`ignored_non_shadow` and left out of every rate.

Human response. Anything a person (`actor_kind` = `person`) recorded on the
incident, with its time:
  - an `ops_action` (time `executed_at`) reached through an outcome's
    `action_id`, through a person-authored proposal on the incident, or
    through the automation proposal itself;
  - an `ops_decision` on the proposal (time `decided_at`);
  - an `ops_outcome` with a person actor, such as a ticket resolution (time
    `checked_at`). It shows a person acted, but not what they did.

Horizon. `horizon_hours` (default 72) after `proposed_at`. A response is
"in horizon" when it is at or after `proposed_at` and no later than the
horizon. Responses before the proposal count only for time-to-first-response.
`as_of` is `until`, else the latest time seen in the records. A proposal whose
horizon ends after `as_of` is not yet final.

Classes, tested in this order (each proposal gets exactly one):
  agree        an in-horizon person action has the proposal's `action_class`
               and equals the proposal's payload on its key fields (all of the
               proposal's payload fields unless `key_fields[action_class]`
               names fewer; extra fields on the human side are ignored), or a
               person approved the proposal in an `ops_decision`.
  disagree     (horizon not ended) an in-horizon person action of another
               class or payload, or a person rejection.
  pending      (horizon not ended) nothing else. Left out of every rate.
  false        (horizon ended) the incident's evidence says no action was
               needed: incident status `false_alarm` or `no_action_needed`, or
               its latest outcome (by `checked_at`) has result
               `no_action_needed`. Only these explicit signals count. A
               resolved incident does not.
  disagree     (horizon ended, not false) as above.
  late         no in-horizon response, but a person responded after the
               horizon (action, decision or outcome), and not false.
  undecidable  the rest: nobody acted in horizon, or only an outcome shows a
               person acted without saying what, and nothing says whether
               action was needed. We do not guess.

Metrics.
  scored = proposals - pending.
  agreement_rate = agree / (agree + disagree): of proposals a person answered
    in horizon, how many matched. None when nobody answered.
  response_coverage = (agree + disagree) / scored.
  false_proposal_rate = false / (scored - undecidable); None when that is 0.
  false_proposal_rate_upper = (false + undecidable) / scored: counts every
    undecidable proposal as false. Readiness checks this one.
  time_to_first_response, per incident, in seconds. `automation`: the
    earliest scored proposal's `proposed_at` minus the incident's
    `detected_at`. `human`: the earliest human response at or after
    `detected_at`, minus `detected_at` (late responses count). Each has n,
    median and p90 (nearest rank). `median_lead_seconds` is the median of
    human minus automation over incidents with both (negative: the person
    was first). Incidents with no `detected_at`, or a proposal before it,
    are left out and counted in `excluded`.

    python -m archi.ops.shadow --deployment D --automation A [--since T] [--until T]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from archi.ops.precedents import Reader, okg_reader
from archi.ops.record import RecordError

DEFAULT_HORIZON_HOURS = 72.0
NO_ACTION_STATUSES = frozenset({"false_alarm", "no_action_needed"})
NO_ACTION_RESULT = "no_action_needed"
CLASSES = ("agree", "disagree", "false", "late", "undecidable", "pending")


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _attrs(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row.get("attrs") or {})


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _ratio(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def _matches(proposal: Mapping[str, Any], action: Mapping[str, Any],
             key_fields: Mapping[str, Sequence[str]]) -> bool:
    if action.get("action_class") != proposal.get("action_class"):
        return False
    wanted = _payload(proposal.get("payload"))
    fields = key_fields.get(proposal.get("action_class") or "")
    if fields is not None:
        wanted = {k: wanted[k] for k in fields if k in wanted}
    got = _payload(action.get("payload"))
    return all(k in got and got[k] == v for k, v in wanted.items())


def _human_responses(read: Reader, proposal: Mapping[str, Any], incident_id: str,
                     decisions: list, outcomes: list) -> list[dict[str, Any]]:
    """Every person response on the incident: kind, time, and the row's attrs."""
    responses: list[dict[str, Any]] = []
    seen_actions: set = set()

    def add_action(row: Mapping[str, Any]) -> None:
        attrs = _attrs(row)
        rid = attrs.get("record_id")
        if attrs.get("actor_kind") != "person" or rid in seen_actions:
            return
        seen_actions.add(rid)
        responses.append({"kind": "action", "at": parse_time(attrs.get("executed_at")), "attrs": attrs})

    for row in read("ops_action", "proposal_id", proposal["record_id"]):
        add_action(row)
    for other in read("ops_proposal", "incident_id", incident_id):
        oattrs = _attrs(other)
        if oattrs.get("actor_kind") == "person":
            for row in read("ops_action", "proposal_id", oattrs["record_id"]):
                add_action(row)
    for outcome in outcomes:
        if outcome.get("action_id"):
            for row in read("ops_action", "record_id", outcome["action_id"]):
                add_action(row)
        if outcome.get("actor_kind") == "person":
            responses.append({"kind": "outcome", "at": parse_time(outcome.get("checked_at")),
                              "attrs": outcome})
    for decision in decisions:
        if decision.get("actor_kind") == "person":
            responses.append({"kind": "decision", "at": parse_time(decision.get("decided_at")),
                              "attrs": decision})
    return [r for r in responses if r["at"] is not None]


def _no_action_needed(incident: Mapping[str, Any], outcomes: list) -> bool:
    if (incident.get("status") or "").lower() in NO_ACTION_STATUSES:
        return True
    dated = [o for o in outcomes if parse_time(o.get("checked_at"))]
    if not dated:
        return False
    latest = max(dated, key=lambda o: parse_time(o["checked_at"]))
    return (latest.get("result") or "").lower() == NO_ACTION_RESULT


def classify(proposal: Mapping[str, Any], responses: list, incident: Mapping[str, Any],
             outcomes: list, *, as_of: datetime, horizon: timedelta,
             key_fields: Mapping[str, Sequence[str]]) -> str:
    start = parse_time(proposal.get("proposed_at"))
    end = start + horizon
    inside = [r for r in responses if start <= r["at"] <= end]
    for r in inside:
        if r["kind"] == "action" and _matches(proposal, r["attrs"], key_fields):
            return "agree"
        if r["kind"] == "decision" and r["attrs"].get("verdict") == "approved":
            return "agree"
    differing = [r for r in inside if r["kind"] == "action"
                 or (r["kind"] == "decision" and r["attrs"].get("verdict") == "rejected")]
    if end > as_of:
        return "disagree" if differing else "pending"
    if _no_action_needed(incident, outcomes):
        return "false"
    if differing:
        return "disagree"
    if any(r["at"] > end for r in responses):
        return "late"
    return "undecidable"


def _summary(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "median": _median(values), "p90": _percentile(values, 0.9)}


def score_automation(
    read: Reader,
    automation: str,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    key_fields: Optional[Mapping[str, Sequence[str]]] = None,
) -> dict[str, Any]:
    """The shadow report for `automation` over [since, until] (JSON-ready)."""
    lo, hi = parse_time(since), parse_time(until)
    if (since and lo is None) or (until and hi is None):
        raise RecordError("since and until must be ISO 8601 timestamps")
    horizon = timedelta(hours=horizon_hours)
    key_fields = key_fields or {}

    ignored = 0
    proposals = []
    for row in read("ops_proposal", "automation", automation):
        attrs = _attrs(row)
        moment = parse_time(attrs.get("proposed_at"))
        if attrs.get("mode") != "shadow":
            ignored += 1
            continue
        if moment is None or (lo and moment < lo) or (hi and moment > hi):
            continue
        proposals.append(attrs)
    proposals.sort(key=lambda p: (parse_time(p["proposed_at"]), p["record_id"]))

    incidents: dict[str, tuple] = {}
    gathered = []
    times = [parse_time(p["proposed_at"]) for p in proposals]
    for p in proposals:
        iid = p["incident_id"]
        if iid not in incidents:
            rows = read("ops_incident", "record_id", iid)
            incidents[iid] = (_attrs(rows[0]) if rows else {},
                              [_attrs(o) for o in read("ops_outcome", "incident_id", iid)])
        incident, outcomes = incidents[iid]
        decisions = [_attrs(d) for d in read("ops_decision", "proposal_id", p["record_id"])]
        responses = _human_responses(read, p, iid, decisions, outcomes)
        times += [r["at"] for r in responses]
        gathered.append((p, responses))
    as_of = hi or max(times, default=datetime.now(timezone.utc))

    counts = {name: 0 for name in CLASSES}
    details = []
    incident_responses: dict[str, list] = {}
    for p, responses in gathered:
        incident, outcomes = incidents[p["incident_id"]]
        label = classify(p, responses, incident, outcomes, as_of=as_of, horizon=horizon,
                         key_fields=key_fields)
        counts[label] += 1
        incident_responses.setdefault(p["incident_id"], []).extend(responses)
        details.append({"proposal_id": p["record_id"], "incident_id": p["incident_id"],
                        "action_class": p.get("action_class"), "proposed_at": p["proposed_at"],
                        "class": label})

    total = len(proposals)
    scored = total - counts["pending"]
    answered = counts["agree"] + counts["disagree"]

    auto_ttfr, human_ttfr, lead, excluded = [], [], [], 0
    first_proposal: dict[str, datetime] = {}
    for p in proposals:
        first_proposal.setdefault(p["incident_id"], parse_time(p["proposed_at"]))
    for iid, proposed in first_proposal.items():
        detected = parse_time(incidents[iid][0].get("detected_at"))
        if detected is None or proposed < detected:
            excluded += 1
            continue
        a = (proposed - detected).total_seconds()
        auto_ttfr.append(a)
        later = [r["at"] for r in incident_responses.get(iid, []) if r["at"] >= detected]
        if later:
            h = (min(later) - detected).total_seconds()
            human_ttfr.append(h)
            lead.append(h - a)

    return {
        "automation": automation,
        "window": {"since": since, "until": until, "as_of": as_of.isoformat().replace("+00:00", "Z")},
        "horizon_hours": horizon_hours,
        "proposals": total,
        "ignored_non_shadow": ignored,
        "scored": scored,
        "counts": counts,
        "agreement_rate": _ratio(counts["agree"], answered),
        "response_coverage": _ratio(answered, scored),
        "false_proposal_rate": _ratio(counts["false"], scored - counts["undecidable"]),
        "false_proposal_rate_upper": _ratio(counts["false"] + counts["undecidable"], scored),
        "time_to_first_response": {
            "automation": _summary(auto_ttfr),
            "human": _summary(human_ttfr),
            "median_lead_seconds": _median(lead),
            "excluded": excluded,
        },
        "details": details,
    }


def promotion_readiness(report: Mapping[str, Any], *, min_proposals: int = 30,
                        min_agreement: float = 0.8, max_false_rate: float = 0.1) -> dict[str, Any]:
    """Is the report good enough to ask an operator to promote? Advice only.

    Promotion itself is a PACT decision recorded in the instance repo.
    Checks scored proposals, agreement_rate, and false_proposal_rate_upper
    (the one that counts undecidable proposals as false). A missing rate fails.
    """
    reasons = []
    if report.get("scored", 0) < min_proposals:
        reasons.append(f"only {report.get('scored', 0)} scored proposals; need {min_proposals}")
    agreement = report.get("agreement_rate")
    if agreement is None:
        reasons.append("no agreement rate: no person answered a proposal in horizon")
    elif agreement < min_agreement:
        reasons.append(f"agreement {agreement:.2f} is below {min_agreement:.2f}")
    false_rate = report.get("false_proposal_rate_upper")
    if false_rate is None:
        reasons.append("no false-proposal rate: nothing was scored")
    elif false_rate > max_false_rate:
        reasons.append(f"false-proposal rate (undecidable counted as false) {false_rate:.2f} "
                       f"is above {max_false_rate:.2f}")
    return {"ready": not reasons, "reasons": reasons}


def _fmt(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _hours(seconds: Any) -> str:
    return "n/a" if seconds is None else f"{seconds / 3600:.1f} h"


def render_markdown(report: Mapping[str, Any]) -> str:
    c, t, w = report["counts"], report["time_to_first_response"], report["window"]
    lines = [
        f"# Shadow report: {report['automation']}",
        "",
        f"Window {_fmt(w['since'])} to {_fmt(w['until'])}, as of {w['as_of']}; "
        f"horizon {report['horizon_hours']:g} h.",
        "",
        f"- Proposals: {report['proposals']} ({report['scored']} scored, {c['pending']} pending, "
        f"{report['ignored_non_shadow']} non-shadow ignored)",
        f"- Agreement: {_fmt(report['agreement_rate'])} "
        f"({c['agree']} agree, {c['disagree']} disagree; coverage {_fmt(report['response_coverage'])})",
        f"- False proposals: {c['false']} (rate {_fmt(report['false_proposal_rate'])}; "
        f"upper bound {_fmt(report['false_proposal_rate_upper'])})",
        f"- Late human response: {c['late']}; undecidable: {c['undecidable']}",
        "",
        "| Time to first response | n | median | p90 |",
        "|---|---|---|---|",
    ]
    for who in ("automation", "human"):
        s = t[who]
        lines.append(f"| {who} | {s['n']} | {_hours(s['median'])} | {_hours(s['p90'])} |")
    lines += ["", f"Median lead of automation over human: {_hours(t['median_lead_seconds'])} "
              f"({t['excluded']} incidents excluded)."]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archi.ops.shadow",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--automation", required=True)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--horizon-hours", type=float, default=DEFAULT_HORIZON_HOURS)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--dsn")
    args = parser.parse_args(argv)
    try:
        report = score_automation(okg_reader(args.deployment, dsn=args.dsn), args.automation,
                                  since=args.since, until=args.until,
                                  horizon_hours=args.horizon_hours)
    except RecordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.format == "json" else render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
