# Cutover runbook: Archi 3.0 becomes `main`

This page is the order of work for the day the 3.0 line replaces v2 as
`archi-physics/archi` `main`, and `archi-kb` 3.0.0 reaches PyPI. It follows
[ADR 0002](../reference/adr.md) decision D1 and the cutover rules of ADR 0001
section 14. Each step has the exact commands, a check that proves it, and a
way back.

**Every step marked `JASON` needs the repository owner**: an admin setting, a
PyPI account, a written decision or an announcement. Nobody else runs
those. A step with no mark can be run by any maintainer with push rights.

Run the steps in order. Do not start step 7 (the branch swap) until steps 1 to 6 are done.

## State of the conditions

ADR 0001 section 14 says the cutover happens when four things hold, "and not
before". Where each stands when 3.0.0 is prepared:

| Condition | State | What closes it |
|---|---|---|
| 1. Every live v2 deployment has a named owner (R1). | **Not answered.** No inventory exists in the repository. | Step 1. |
| 2. Each of those deployments is migrated or stays on a tagged `v2.x`. | Not started. | Step 3. |
| 3. Parity for comp-ops and the chat migration hold, or the losses are accepted in writing. | **Not met.** The `cms-comp-ops` bundle, `ticket-triage` and its four-week shadow report are not in 3.0.0. See [what has no equivalent yet](../migration-from-v2.md#what-has-no-equivalent-yet). | Step 2 (the written acceptance), `JASON`. |
| 4. The last v2 release is tagged and documented. | The newest tag is `v2.5.0`. `main` has had 52 commits since (check: step 0). | Step 4. |

State today of the repository settings this runbook changes (read with `gh`
on 2026-10-26, so re-check at the time):

- the default branch is `main`, and it is protected: one approving review,
  no force pushes, no deletions;
- `archi_v3` is not protected;
- there is no `v2` branch;
- the repository has no GitHub environment named `pypi`;
- `archi-kb` has no pending publisher on PyPI. (Nobody has looked at
  pypi.org for this page. Check it in step 6.)

## Step 0. Record the starting point

Any maintainer. Read-only.

```bash
git clone https://github.com/archi-physics/archi.git archi-cutover && cd archi-cutover
git fetch --tags origin
V2_TIP=$(git rev-parse origin/main);      echo "v2 tip  $V2_TIP"
V3_TIP=$(git rev-parse origin/archi_v3);  echo "v3 tip  $V3_TIP"
git tag -l 'v2.*' --sort=-v:refname | head -3
git log --oneline "$(git tag -l 'v2.*' --sort=-v:refname | head -1)..origin/main" | wc -l
gh pr list --repo archi-physics/archi --base main --state open --json number --jq length
grep '^version' python/pyproject.toml   # on archi_v3: must say 3.0.0 by step 5
```

Write the two tip SHAs in the cutover issue. Every rollback below checks
against them.

**Check:** both SHAs recorded; the newest v2 tag and the count of commits
after it are known; you know how many pull requests are open against `main`.

## Step 1. Inventory the live v2 deployments (R1)

`JASON` decides who is asked. Anyone can gather the answers.

For each machine that may run v2, on that machine:

```bash
archi list-deployments          # v2 command; run it in the v2 environment
```

Record, in one table in the cutover issue: deployment name, host, services
(`chatbot`, `uploader`, queue bots), the v2 tag or commit it runs, the owner
(a named person), and the decision: **move to 3.0** or **stay on a tagged
`v2.x`**. Ask the people who run `archi create` on shared machines and on
SubMIT. A deployment nobody claims is a deployment nobody can move.

**Check:** every row has a named owner and a decision.
**Rollback:** nothing to undo.

## Step 2. Accept the losses in writing

`JASON`. Condition 3 holds only with parity, or with the losses accepted in
writing. 3.0.0 does not have parity. Write the acceptance and keep it with the
inventory. It lists, from [the 3.0 limits](../migration-from-v2.md#what-has-no-equivalent-yet):

- the `cms-comp-ops` and `cluster-manager` bundles are not shipped;
- `ticket-triage` is not shipped and has no four-week shadow report, so the v2
  queue bots stay on a tagged `v2.x`;
- `archi eval` and the bundle evals are not shipped;
- no upload, Redmine, SSO-page or Helm equivalents.

**Check:** the written acceptance is linked from the cutover issue and from the
`r3-release` PACT evidence.
**Rollback:** nothing to undo. Without the acceptance, stop here.

## Step 3. Move or pin each v2 deployment

Each owner, on the host. This is the real migration. Every deployment in the
inventory takes one of two routes.

**Route A. Move to 3.0.** Follow [Moving from v2 to 3.0](../migration-from-v2.md):
back up v2, stand 3.0 up beside it, move the sources, compare answers. v2 stays
up until the owner is satisfied.

**Route B. Stay on a tagged v2 release.** Pin the deployment to the tag, so a
later change on the v2 branch cannot reach it:

```bash
cd <the v2 checkout>
git fetch --tags origin
git checkout v2.5.0                    # or the tag step 4 creates
pip install -e .                       # in the v2 Python environment
archi restart -n <name> --service chatbot
```

(`archi restart` rebuilds and restarts one service. Repeat for each service the
deployment runs. See the [v2 CLI reference](../v2/cli_reference.md#archi-restart).)

**Check:** for a pinned deployment, `git describe --tags` in its checkout prints the
tag, and the service answers a question. For a moved one, `archi doctor`
exits 0 on the new instance and its owner signs off on the comparison.
**Rollback:** a moved deployment: `archi down --purge --yes` on the 3.0
instance, v2 is untouched. A pinned one: `git checkout` the previous tag or
commit and `archi restart` again.

## Step 4. Tag the last v2 release

Any maintainer with push rights, after the owners in step 3 have chosen their
tags. Skip if the newest tag is already the release you want and `main` has
no commits after it.

```bash
git fetch origin
git tag -a v2.6.0 "$V2_TIP" -m "Last v2 release before the 3.0 cutover"
git push origin v2.6.0
gh release create v2.6.0 --repo archi-physics/archi --target "$V2_TIP" \
    --title "v2.6.0 (last v2 release)" \
    --notes "The last v2 release. From the cutover this line lives on the v2 branch. See docs/docs/migration-from-v2.md."
```

(Pick the next number if `v2.6.0` is taken: check step 0's list.)

**Check:** `gh release view v2.6.0 --repo archi-physics/archi` shows it, and
`git rev-parse v2.6.0^{commit}` equals `$V2_TIP`.
**Rollback:** `gh release delete v2.6.0 --repo archi-physics/archi --yes` and
`git push origin :refs/tags/v2.6.0`. (Deleting a release or tag is destructive.
`JASON` approves it.) Do it only if nobody has pinned the tag.

## Step 5. Merge the release-readiness change into `archi_v3`

The pull request that holds this page also bumps the version to 3.0.0. A
maintainer merges it after CI is green, not before:

```bash
gh pr checks <number> --repo archi-physics/archi
gh pr merge <number> --repo archi-physics/archi --merge --match-head-commit <sha>
```

Use `--merge`, never `--squash`: a squash drops the approval trailers PACT
reads. `archi_v3` has no required checks, so the merge happens at once. That is why
you check first.

Then wait for the other pending work you want in 3.0.0 and stop merging.
Freeze `main` and `archi_v3`: announce it in the cutover issue.

**Check:** on `archi_v3`, `grep '^version' python/pyproject.toml` prints
`version = "3.0.0"`, and `grep __version__ python/archi/__init__.py` prints
the same.
**Rollback:** `git revert -m 1 <merge sha>` on `archi_v3`, as a pull request.

## Step 6. Prepare PyPI (once)

`JASON`. Nothing here can be done from the repository. The trusted
publisher is how `release.yml` publishes without a token.

1. On pypi.org, signed in as the owner: **Your projects, Publishing, Add a new
   pending publisher**. Enter:

   | Field | Value |
   |---|---|
   | PyPI project name | `archi-kb` |
   | Owner | `archi-physics` |
   | Repository name | `archi` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

2. On GitHub, create the environment the workflow names. Add yourself as a
   required reviewer if you want a pause before each publish:

   ```bash
   gh api --method PUT repos/archi-physics/archi/environments/pypi
   gh api repos/archi-physics/archi/environments --jq '.environments[].name'
   ```

   (Or use Settings, Environments, New environment, `pypi`.)

3. Check the build before it matters. In a scratch directory, outside the
   repository:

   ```bash
   git clone --branch archi_v3 https://github.com/archi-physics/archi.git /tmp/archi-wheel-check
   cd /tmp/archi-wheel-check/python
   python -m venv /tmp/archi-wheel-venv && . /tmp/archi-wheel-venv/bin/activate
   pip install build && python -m build --wheel --outdir /tmp/archi-wheel-dist
   ls /tmp/archi-wheel-dist        # archi_kb-3.0.0-py3-none-any.whl
   ```

**Check:** the environment name prints `pypi`; the wheel is named
`archi_kb-3.0.0-py3-none-any.whl`. The pending publisher is checked by
the first publish in step 8.
**Rollback:** `gh api --method DELETE repos/archi-physics/archi/environments/pypi`;
delete the pending publisher on pypi.org. Nothing has been published.

## Step 7. Swap the branches

`JASON`. This is the cutover itself. It needs repository admin, and the
rename API keeps open pull requests: those that target `main` follow it
to `v2`.

```bash
# 1. main becomes v2. The default branch moves with it.
gh api --method POST repos/archi-physics/archi/branches/main/rename -f new_name=v2
# 2. archi_v3 becomes main.
gh api --method POST repos/archi-physics/archi/branches/archi_v3/rename -f new_name=main
# 3. The default branch is the new main.
gh repo edit archi-physics/archi --default-branch main
```

If a rename is refused because of branch protection, remove the protection
for the minute it takes in Settings, Branches, run the rename, and put it
back (step 7b).

**Step 7b. Protect both branches again.** Protection belongs to a branch
name. Check what each name has now:

```bash
gh api repos/archi-physics/archi/branches/main/protection --jq '.required_pull_request_reviews.required_approving_review_count'
gh api repos/archi-physics/archi/branches/v2/protection --jq '.required_pull_request_reviews.required_approving_review_count'
```

Set the rules you want on `main` (the 3.0 line) and `v2` in Settings,
Branches. v2 should keep one approving review and no force pushes.

**Step 7c. Tell local clones.** In every clone of the repository:

```bash
git fetch origin
git branch -m main v2 && git branch -u origin/v2 v2          # a clone that had main (v2)
git branch -m archi_v3 main && git branch -u origin/main main  # a clone that had archi_v3
git remote set-head origin -a
```

**Check:**

```bash
gh api repos/archi-physics/archi --jq .default_branch                       # main
git ls-remote --heads origin main v2 archi_v3
test "$(git rev-parse origin/main)" = "$V3_TIP" && echo "main is the v3 line"
test "$(git rev-parse origin/v2)"   = "$V2_TIP" && echo "v2 holds the v2 line"
```

`archi_v3` must no longer be listed. The two `test` lines must both print.

**Rollback:** swap them back, in the reverse order:

```bash
gh api --method POST repos/archi-physics/archi/branches/main/rename -f new_name=archi_v3
gh api --method POST repos/archi-physics/archi/branches/v2/rename   -f new_name=main
gh repo edit archi-physics/archi --default-branch main
```

Then run the same check with the two names swapped, and restore protection as
in step 7b. Open pull requests follow the renames again.

## Step 8. Fix the workflow triggers

A maintainer, in a pull request on the new `main`. These edits are in
`.github/workflows/`, which the release-readiness change does not touch:

- `ci.yml`: the push trigger lists `[main, archi_v3]`. Drop `archi_v3`.
- `nightly-e2e.yml`: the push trigger lists `archi_v3`. Change it to `main`.
- `deploy-docs.yml`: it deploys the site on a push to `main`. The rename is not
  a push, so run it once by hand:

  ```bash
  gh workflow run deploy-docs.yml --repo archi-physics/archi --ref main
  gh run list --workflow deploy-docs.yml --repo archi-physics/archi --limit 1
  ```

The workflows on the `v2` branch name `main`. They do not run on `v2` until
a v2 pull request changes them. Do that when the first v2 fix needs CI.

**Check:** the next push to `main` runs `ci`; the site at
<https://archi-physics.github.io/archi/> shows the 3.0 pages.
**Rollback:** revert the pull request.

## Step 9. Tag 3.0.0 and publish

`JASON` for the tag, because pushing it publishes to PyPI at once and a
published version cannot be reused. Only after step 7's checks pass.

```bash
git fetch origin
git switch main && git pull --ff-only
grep '^version' python/pyproject.toml          # version = "3.0.0"
git tag -a v3.0.0 -m "archi-kb 3.0.0"
git push origin v3.0.0
```

The tag starts `release.yml`. It refuses when the tag differs from the version
in `python/pyproject.toml` or in `archi.__version__`. It builds the wheel, then
publishes through the `pypi` environment. Watch it:

```bash
gh run list --workflow release.yml --repo archi-physics/archi --limit 1
gh run watch --repo archi-physics/archi "$(gh run list --workflow release.yml --repo archi-physics/archi --limit 1 --json databaseId --jq '.[0].databaseId')"
```

If you made yourself a required reviewer in step 6, approve the `publish` job
in the run.

Then create the GitHub release from the release notes:

```bash
gh release create v3.0.0 --repo archi-physics/archi --verify-tag \
    --title "archi 3.0.0" --notes-file docs/release-notes-3.0.0.md
```

**Check (this is the `r3-release` acceptance):** in an empty directory and a
new environment:

```bash
python3 -m venv /tmp/archi-300 && . /tmp/archi-300/bin/activate
pip install "okg @ https://github.com/mitdbg/akmon/releases/download/v0.2.0/okg-0.2.0-py3-none-any.whl" archi-kb==3.0.0
python -c "import archi; print(archi.__version__)"     # 3.0.0
archi init cutover-check --dir /tmp/archi-300-instance
archi doctor --instance /tmp/archi-300-instance
archi down --purge --yes --instance /tmp/archi-300-instance
```

`archi doctor` must exit 0. Record the output in the `r3-release` PACT evidence.

**Rollback.** A published version stays on PyPI.

- If the `build` job failed, nothing was published. Delete the tag
  (`git push origin :refs/tags/v3.0.0`), fix, and tag again.
- If `publish` ran, do not reuse the number. `JASON` yanks the release on
  pypi.org (Manage, Releases, Options, Yank), fixes the problem, and
  releases `3.0.1`. Delete the GitHub release
  (`gh release delete v3.0.0 --yes`) only if you also delete the tag, and only
  when nobody has installed it.

## Step 10. Announce

`JASON` sends it. Include: where to start ([quickstart](../quickstart.md)),
the [release notes](https://github.com/archi-physics/archi/blob/main/docs/release-notes-3.0.0.md),
the supported AKMON version (okg 0.2.0), what 3.0.0 does not have (step 2), the
branch names (`main` is 3.0, `v2` is v2), and the date the v2 branch stops
receiving fixes. That date is set from the inventory in step 1, not before it.

Also: point the archi-okg README at this repository if it does not already,
and close the `3.0 · M7` milestone once the `r3-docs` and `r3-release` PACTs
are approved.

**Rollback:** send a correction.

## If something goes wrong after the cutover

| Problem | Do this |
|---|---|
| A v2 owner needs a fix. | Open the pull request against the `v2` branch. Tag `v2.<n>`. The owner pins the new tag as in step 3, route B. |
| A v2 deployment must be rebuilt. | `git checkout v2.6.0` (or its pinned tag), then the restore steps in [Moving from v2 to 3.0](../migration-from-v2.md#roll-back). |
| Someone's clone is confused by the renames. | Step 7c, or a fresh clone. |
| 3.0.0 has a bad bug. | Fix on `main`, bump to `3.0.1`, tag `v3.0.1`. Yank 3.0.0 only if it is harmful. |
| The branch swap itself was wrong. | Step 7's rollback. It is safe until people push to the new `main`. After that, talk first: a swap back loses nothing, but it moves their pull requests again. |
