"""The two embedding containers, and only those, get a bounded torch thread count.

The data-manager embeds every chunk at ingest and the chatbot embeds every search
query. Both run torch on CPU, which without ``OMP_NUM_THREADS`` starts one thread
per host core. Other services do not embed and are left alone.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

from src.cli.managers import templates_manager
from src.cli.managers.templates_manager import TemplateContext, TemplateManager
from src.cli.utils.service_builder import ServiceBuilder

CAPPED_BLOCKS = ("data-manager", "chatbot")
UNCAPPED_BLOCKS = ("grader", "piazza", "mattermost", "redmine", "mailbox", "benchmark")


def _render_compose(tmp_path, **extra_vars):
    repository = Path(__file__).resolve().parents[2]
    environment = Environment(
        loader=FileSystemLoader(str(repository / "src/cli/templates")),
        undefined=ChainableUndefined,
    )
    plan = ServiceBuilder.build_compose_config(
        name="demo",
        verbosity=3,
        base_dir=tmp_path,
        enabled_services=[
            "chatbot",
            "data-manager",
            "grader",
            "piazza",
            "mattermost",
            "redmine-mailer",
            "benchmarking",
        ],
        secrets={"PG_PASSWORD"},
        tag="dev",
    )
    template_vars = plan.to_template_vars()
    template_vars.update(
        app_version="test",
        postgres_port=5432,
        data_manager_port_host=7871,
        data_manager_port_container=7871,
        chatbot_port_host=7861,
        chatbot_port_container=7861,
        grader_port_host=7862,
        grader_port_container=7862,
        prompt_files=[],
        rubrics=[],
        **extra_vars,
    )
    rendered = environment.get_template("base-compose.yaml").render(**template_vars)
    return yaml.safe_load(rendered)["services"]


def _env(services, block):
    assert block in services, f"{block} is missing from the rendered compose file"
    return services[block].get("environment") or {}


@pytest.mark.parametrize("threads", [8, 4])
@pytest.mark.parametrize("block", CAPPED_BLOCKS)
def test_embedding_containers_get_the_thread_count(tmp_path, block, threads):
    services = _render_compose(tmp_path, torch_threads=threads)

    assert _env(services, block).get("OMP_NUM_THREADS") == str(threads)


@pytest.mark.parametrize("block", UNCAPPED_BLOCKS)
def test_other_services_get_no_thread_count(tmp_path, block):
    services = _render_compose(tmp_path, torch_threads=8)
    if block not in services:
        pytest.skip(f"{block} is not rendered in this configuration")

    assert "OMP_NUM_THREADS" not in _env(services, block)


@pytest.mark.parametrize("block", CAPPED_BLOCKS)
def test_no_value_renders_no_variable(tmp_path, block):
    """A caller that renders without the value must not ship OMP_NUM_THREADS: ''."""
    services = _render_compose(tmp_path)

    assert "OMP_NUM_THREADS" not in _env(services, block)


def _render_through_manager(tmp_path, monkeypatch, data_manager):
    """Drive the real _render_compose_file with a config carrying ``data_manager``."""
    repository = Path(__file__).resolve().parents[2]
    manager = TemplateManager(
        jinja_env=Environment(
            loader=FileSystemLoader(str(repository / "src/cli/templates")),
            undefined=ChainableUndefined,
        ),
        verbosity=0,
    )
    monkeypatch.setattr(manager, "_check_ports_available", lambda *a, **k: None)
    monkeypatch.setattr(templates_manager, "get_git_version", lambda: "test")
    config = {"services": {}, "data_manager": data_manager}
    plan = ServiceBuilder.build_compose_config(
        name="demo",
        verbosity=0,
        base_dir=tmp_path,
        enabled_services=["chatbot", "data-manager"],
        secrets={"PG_PASSWORD"},
        tag="dev",
    )
    context = TemplateContext(
        plan=plan,
        config_manager=SimpleNamespace(config=config, get_configs=lambda: [config]),
        secrets_manager=MagicMock(),
        options={},
    )
    manager._render_compose_file(context)
    return yaml.safe_load((tmp_path / "compose.yaml").read_text())["services"]


@pytest.mark.parametrize(
    ("data_manager", "expected"), [({}, "8"), ({"torch_threads": 4}, "4")]
)
def test_render_compose_file_writes_the_resolved_count(
    tmp_path, monkeypatch, data_manager, expected
):
    services = _render_through_manager(tmp_path, monkeypatch, data_manager)

    for block in CAPPED_BLOCKS:
        assert _env(services, block).get("OMP_NUM_THREADS") == expected


def test_render_compose_file_refuses_an_invalid_count(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match=r"data_manager\.torch_threads"):
        _render_through_manager(tmp_path, monkeypatch, {"torch_threads": 0})

    assert not (tmp_path / "compose.yaml").exists()
