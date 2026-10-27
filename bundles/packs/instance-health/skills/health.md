# Instance health

The `instance-health` automation watches this instance. It runs on a schedule (hourly), with no model call.

**What it reads**

- `archi doctor --json`: every failing check. `source-freshness` yields one problem per late source.
- `okg sync frontier --json`: a source whose newest ingest failed or whose frontier is blocked.
- The ops records: an automation whose recent decisions are mostly errors.
- The `report.json` files `archi eval` writes under `<data root>/eval/` (or `eval.reports_dir` in `ops.yaml`).
  Per bundle, the latest run is a problem (`eval/<bundle>`) when it ended `execution_failed` (the run could not
  ask the instance: a failed signal, not a low score), when it is below the bundle's threshold, or when its
  score fell more than `eval.min_drop` (default 0.1) below the earlier runs of the same model. With no report,
  this is skipped, and the proposal says so; a bundle with only one run is judged on its threshold alone.

**What it does**

It keeps one issue per problem, found by a stable key (`<check>/<subject>`, for example
`source-freshness/jira.jira`). A problem seen again updates its issue. A problem that clears marks the issue
resolved. A problem that returns reopens the same issue.

At L1 the issues are Markdown files in `.archi/issues/` of the instance repository, committed there. Read them
with `ls .archi/issues/` and `git log -- .archi/issues/`. If a signal could not be read, the problem
`monitor/<signal>` says so, and that signal's own issues are left alone until it can be read again.

GitLab and GitHub issues need the pack raised to L2 and an approval for each sync; see docs/docs/reference/pack-format.md.
