"""`archi init` and `archi up` with an automation pack selected need no hand step: the DBOS schema is
bootstrapped when provision stops at its ingest stage (okg upstream ask O12), and `up` repeats the
bootstrap, which is idempotent."""
import subprocess

import pytest

from archi.cli import init as init_mod
from archi.cli import up as up_mod
from archi.stack.okg import OkgError

from test_stack import _runner, instance  # noqa: F401  (the fixture and the fake compose runner)

DBOS_MISSING = 'relation "dbos.workflow_status" does not exist'


class Okg:
    """Stands in for `Okg.run`: provision fails with the DBOS error until the runtime is bootstrapped."""

    def __init__(self, bootstrapped=False, provision_error=DBOS_MISSING):
        self.calls, self.bootstrapped, self.provision_error = [], bootstrapped, provision_error

    def run(self, *args, ok_codes=(0,), **kw):
        self.calls.append(args)
        code, err = 0, ""
        if args[0] == "provision" and not self.bootstrapped:
            code, err = 1, self.provision_error
        if args[:2] == ("runtime", "bootstrap"):
            self.bootstrapped = True
        result = subprocess.CompletedProcess(args, code, "", err)
        if code not in ok_codes:
            raise OkgError(f"`okg {' '.join(args)}` failed ({code}):\n{err}", result)
        return result


def test_provision_bootstraps_the_runtime_then_ingests_in_process_and_publishes_when_dbos_is_missing():
    okg, said = Okg(), []
    result = init_mod.provision(okg, "dep", runtime=True, echo=said.append)
    assert result.returncode == 0
    assert okg.calls == [("provision", "--deployment", "dep"), ("runtime", "bootstrap", "--deployment", "dep"),
                         ("ingest", "--deployment", "dep", "--inline"),
                         ("run", "--once", "--deployment", "dep", "--apply")]
    assert any("bootstrapping the runtime" in line for line in said)


def test_provision_without_the_runtime_is_one_call_and_any_failure_is_raised():
    okg = Okg()
    with pytest.raises(OkgError):
        init_mod.provision(okg, "dep", runtime=False, echo=lambda _: None)
    assert okg.calls == [("provision", "--deployment", "dep")]


def test_a_provision_failure_that_is_not_the_dbos_schema_is_raised_not_bootstrapped_around():
    okg = Okg(provision_error="publish blocked: invariant docsite_coverage failed")
    with pytest.raises(OkgError, match="docsite_coverage"):
        init_mod.provision(okg, "dep", runtime=True, echo=lambda _: None)
    assert [c[0] for c in okg.calls] == ["provision"]


def test_provision_does_not_bootstrap_when_it_already_works():
    okg = Okg(bootstrapped=True)
    init_mod.provision(okg, "dep", runtime=True, echo=lambda _: None)
    assert okg.calls == [("provision", "--deployment", "dep")]


def test_has_automations_reads_the_lock(tmp_path, monkeypatch):
    from archi.bundles import lock

    assert init_mod.has_automations(tmp_path) is False  # no lock: nothing rendered
    monkeypatch.setattr(lock, "read_lock", lambda d: {"composed": {"automations": {"triage": {}}}})
    assert init_mod.has_automations(tmp_path) is True
    monkeypatch.setattr(lock, "read_lock", lambda d: {"composed": {"automations": {}}})
    assert init_mod.has_automations(tmp_path) is False


def test_up_bootstraps_the_runtime_once_the_database_is_ready_when_an_automation_is_selected(monkeypatch, instance):
    from archi.stack import compose as stack_compose
    from archi.stack import webui

    monkeypatch.setattr(up_mod, "commit_all", lambda d, m: False)
    monkeypatch.setattr(up_mod, "has_automations", lambda d: True)
    monkeypatch.setattr(stack_compose, "wait_for", lambda *a, **k: None)
    monkeypatch.setattr(stack_compose, "http_status", lambda url, **k: 0)
    monkeypatch.setattr(webui, "admin_token", lambda *a, **k: "tok")
    calls, lines = [], []

    def okg(cmd, cwd=None, env=None, **kw):
        calls.append(cmd[1:3])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    up_mod.up(instance, echo=lines.append, compose_runner=_runner({}), okg_runner=okg)
    assert calls[0] == ["runtime", "bootstrap"] and ["chat", "sync"] in calls
    assert any("no runtime worker yet" in line for line in lines)


def test_up_without_an_automation_does_not_touch_the_runtime(monkeypatch, instance):
    from archi.stack import compose as stack_compose
    from archi.stack import webui

    monkeypatch.setattr(up_mod, "commit_all", lambda d, m: False)
    monkeypatch.setattr(up_mod, "has_automations", lambda d: False)
    monkeypatch.setattr(stack_compose, "wait_for", lambda *a, **k: None)
    monkeypatch.setattr(stack_compose, "http_status", lambda url, **k: 0)
    monkeypatch.setattr(webui, "admin_token", lambda *a, **k: "tok")
    calls = []
    up_mod.up(instance, echo=lambda _: None, compose_runner=_runner({}),
              okg_runner=lambda cmd, **kw: calls.append(cmd[1:3]) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert ["runtime", "bootstrap"] not in calls
