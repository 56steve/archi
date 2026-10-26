"""req.r3-mcp-clients.connect: each recipe's output, merge and idempotency, no token by default."""
import json
import stat
import tomllib

import pytest

from archi.bundles.cli import main as archi_main
from archi.cli import connect

from connect.conftest import EXTERNAL, TOKEN, make_instance

SUFFIX = {"claude-code": "json", "openclaw": "json", "mcp": "json", "codex": "toml"}


def run_cli(capsys, *argv):
    code = archi_main(["connect", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("client", ["claude-code", "codex", "openclaw", "mcp"])
def test_print_matches_the_golden_file_and_carries_no_token(client, instance, capsys, check_golden):
    code, out, err = run_cli(capsys, client, "--instance", str(instance.dir), "--print")
    assert code == 0, err
    assert TOKEN not in out and TOKEN not in err
    check_golden(f"print-{client}.txt", out)


def test_print_is_the_default_and_show_token_adds_the_token(instance, capsys):
    code, out, _ = run_cli(capsys, "mcp", "--instance", str(instance.dir))
    assert code == 0 and TOKEN not in out
    code, out, _ = run_cli(capsys, "mcp", "--instance", str(instance.dir), "--show-token")
    assert f"Bearer {TOKEN}" in out
    # the external server's secret is a variable reference, never a value
    assert "${NOTES_API_KEY}" in out


def test_codex_names_the_token_variable_and_never_embeds_it(instance, capsys, tmp_path):
    code, out, _ = run_cli(capsys, "codex", "--instance", str(instance.dir), "--show-token")
    assert code == 0
    doc = tomllib.loads("\n".join(l for l in out.splitlines() if not l.startswith("#")))
    server = doc["mcp_servers"]["archi-demo"]
    assert server["url"] == "http://127.0.0.1:18080/mcp"
    assert server["bearer_token_env_var"] == "OKG_CHAT_MCP_TOKEN" and "http_headers" not in server
    assert doc["mcp_servers"]["notes"]["env_http_headers"] == {"X-Api-Key": "NOTES_API_KEY"}
    assert f"export OKG_CHAT_MCP_TOKEN={TOKEN}" in out  # only because --show-token


def test_claude_code_matches_what_claude_mcp_add_writes(instance, capsys):
    """`claude mcp add --transport http NAME URL --header 'H: V'` writes type/url/headers (checked live)."""
    _, out, _ = run_cli(capsys, "claude-code", "--instance", str(instance.dir))
    doc = json.loads(out.split("\n\n# ")[0])
    assert doc["mcpServers"]["archi-demo"] == {
        "type": "http", "url": "http://127.0.0.1:18080/mcp",
        "headers": {"Authorization": "Bearer ${OKG_CHAT_MCP_TOKEN}"},
    }
    assert 'claude mcp add --transport http archi-demo http://127.0.0.1:18080/mcp' in out


def test_openclaw_uses_mcp_servers_with_a_transport_and_says_it_is_unverified(instance, capsys):
    _, out, _ = run_cli(capsys, "openclaw", "--instance", str(instance.dir))
    doc = json.loads(out.split("\n\n# ")[0])
    assert doc["mcp"]["servers"]["archi-demo"]["transport"] == "streamable-http"
    assert "not run it against a live OpenClaw" in out or "has not run it against a live OpenClaw" in out


def test_open_webui_prints_what_okg_chat_sync_registered(instance, capsys):
    instance.deployment_dir.mkdir(parents=True)
    (instance.deployment_dir / "deployment.yaml").write_text("chat:\n  mcp:\n    port: 8000\n")
    code, out, err = run_cli(capsys, "open-webui", "--instance", str(instance.dir))
    assert code == 0, err
    assert "http://mcp:8000/mcp" in out and "http://127.0.0.1:13000" in out and "okg chat sync" in out and TOKEN not in out
    assert "notes" in out  # external servers are listed for a manual add
    code, _, err = run_cli(capsys, "open-webui", "--instance", str(instance.dir), "--write", "x.json")
    assert code == 1 and "no config file" in err


@pytest.mark.parametrize("client", ["claude-code", "openclaw", "mcp"])
def test_write_creates_a_0600_file_holding_the_token(client, instance, tmp_path, capsys, check_golden):
    target = tmp_path / "out" / "cfg.json"
    code, out, err = run_cli(capsys, client, "--instance", str(instance.dir), "--write", str(target))
    assert code == 0, err
    assert TOKEN not in out  # the report never echoes it
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    check_golden(f"write-{client}.json", target.read_text())


def test_write_codex_is_0600_and_holds_no_token(instance, tmp_path, capsys, check_golden):
    target = tmp_path / ".codex" / "config.toml"
    code, out, err = run_cli(capsys, "codex", "--instance", str(instance.dir), "--write", str(target))
    assert code == 0, err
    assert TOKEN not in target.read_text() and TOKEN not in out
    check_golden("write-codex.toml", target.read_text())


def test_token_ref_writes_a_reference_not_the_token(instance, tmp_path, capsys):
    target = tmp_path / ".mcp.json"
    run_cli(capsys, "claude-code", "--instance", str(instance.dir), "--write", str(target), "--token-ref")
    assert TOKEN not in target.read_text()
    assert "Bearer ${OKG_CHAT_MCP_TOKEN}" in target.read_text()


@pytest.mark.parametrize("client", ["claude-code", "openclaw", "mcp", "codex"])
def test_merge_keeps_other_entries_and_is_idempotent(client, instance, tmp_path, capsys):
    target = tmp_path / f"existing.{SUFFIX[client]}"
    if client == "codex":
        target.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "npx"\nargs = ["x"]\n\n[profiles.p]\nmodel = "m"\n')
    elif client == "openclaw":
        target.write_text(json.dumps({"gateway": {"port": 1}, "mcp": {"servers": {"other": {"url": "http://o/mcp"}}}}))
    else:
        target.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "npx"}}}))
    target.chmod(0o644)
    args = (client, "--instance", str(instance.dir), "--write", str(target))
    code, out, _ = run_cli(capsys, *args)
    assert code == 0 and out.startswith("added:")
    first = target.read_text()
    assert "other" in first and ("gpt-5" in first or "theme" in first or "gateway" in first)
    if client == "codex":
        doc = tomllib.loads(first)
        assert doc["profiles"]["p"]["model"] == "m" and doc["mcp_servers"]["other"]["command"] == "npx"
        assert set(doc["mcp_servers"]) == {"other", "archi-demo", "notes"}
    else:
        # an existing file that now holds the token is tightened to 0600
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    code, out, _ = run_cli(capsys, *args)
    assert code == 0 and out.startswith("unchanged:")
    assert target.read_text() == first


def test_codex_merge_replaces_a_stale_archi_entry(instance, tmp_path, capsys):
    target = tmp_path / "config.toml"
    run_cli(capsys, "codex", "--instance", str(instance.dir), "--write", str(target))
    moved = make_instance(tmp_path / "again", mcp_port=19999, external=[EXTERNAL])
    code, out, _ = run_cli(capsys, "codex", "--instance", str(moved.dir), "--write", str(target))
    assert code == 0 and out.startswith("updated:")
    doc = tomllib.loads(target.read_text())
    assert doc["mcp_servers"]["archi-demo"]["url"] == "http://127.0.0.1:19999/mcp"
    assert target.read_text().count("[mcp_servers.archi-demo]") == 1


def test_a_config_archi_cannot_parse_is_left_alone(instance, tmp_path, capsys):
    json5 = tmp_path / "openclaw.json"
    json5.write_text("{ // comment\n  gateway: { port: 1, },\n}\n")
    code, _, err = run_cli(capsys, "openclaw", "--instance", str(instance.dir), "--write", str(json5))
    assert code == 1 and "not strict JSON" in err
    assert json5.read_text().startswith("{ // comment")
    bad = tmp_path / "bad.toml"
    bad.write_text("[oops\n")
    code, _, err = run_cli(capsys, "codex", "--instance", str(instance.dir), "--write", str(bad))
    assert code == 1 and "not valid TOML" in err and bad.read_text() == "[oops\n"


def test_a_missing_token_is_an_error_not_an_empty_header(tmp_path, capsys):
    instance = make_instance(tmp_path)
    (instance.dir / ".env").write_text("ARCHI_ADMIN_PASSWORD=pw\n")
    code, _, err = run_cli(capsys, "mcp", "--instance", str(instance.dir))
    assert code == 1 and "OKG_CHAT_MCP_TOKEN" in err


def test_connect_is_listed_in_archi_help(capsys):
    with pytest.raises(SystemExit):
        archi_main(["--help"])
    assert "connect" in capsys.readouterr().out
    assert connect.CLIENTS == ("open-webui", "claude-code", "codex", "openclaw", "mcp")
