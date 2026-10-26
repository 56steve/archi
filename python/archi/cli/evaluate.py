"""`archi eval [--bundle X] [--instance DIR] [--model M] [--model-url URL] [--out DIR]`.

Runs a bundle's smoke set (`bundles/<bundle>/eval/smoke.yaml`) against the
instance's MCP endpoint with a local model, writes report.md and report.json,
and exits non-zero below the bundle's pass threshold (PACT r3-bundle-evals).

Exit codes: 0 the threshold was met; 1 it was not; 2 the run could not
execute (no MCP tools, the endpoint or the model unreachable, a bad smoke
file). Checks are deterministic: no LLM judge, no paid model call.

This module only builds the parser. The harness (`archi.evaluation`, and the
MCP client it loads) is imported when the command runs, so every other
`archi` command stays as fast as before.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_MODEL_URL = "http://127.0.0.1:11434/v1"
TOKEN_VAR = "OKG_CHAT_MCP_TOKEN"
EVAL_HELP = "ask a bundle's smoke questions through the instance MCP with a local model"


def add_parser(sub, roots) -> None:
    p = sub.add_parser(
        "eval",
        help=EVAL_HELP,
        description=(
            "Run a bundle's smoke set against the instance MCP endpoint with a local "
            "model (an OpenAI-compatible endpoint such as Ollama). Checks are "
            "deterministic; nothing is paid. Exits 1 below the bundle's pass "
            "threshold and 2 when the run could not execute."
        ),
    )
    p.add_argument("--bundle", action="append", default=[],
                   help="bundle whose smoke set to run (repeatable; default: the instance's bundles)")
    p.add_argument("--instance", type=Path, default=Path("."), help="instance directory (default: .)")
    p.add_argument("--model", help="model name at the endpoint (default: the bundle's chat.default_model)")
    p.add_argument("--model-url", default=os.environ.get("ARCHI_EVAL_MODEL_URL", DEFAULT_MODEL_URL),
                   help=f"OpenAI-compatible base URL (default: {DEFAULT_MODEL_URL}, a local Ollama)")
    p.add_argument("--out", type=Path, help="report directory (default: <data root>/eval/<bundle>, outside the instance repository)")
    p.add_argument("--mcp-url", help="MCP endpoint (default: the instance's, http://127.0.0.1:<port>/mcp)")
    p.add_argument("--chat-prompt", type=Path,
                   help="the chat system prompt to give the agent (default: the instance deployment's "
                        "skills/chat-system-prompt.md, the one `archi up` gives the chat)")
    p.add_argument("--max-steps", type=int, default=8, help="tool-call rounds allowed per question")
    p.add_argument("--no-think", action="store_true",
                   help="ask a reasoning model to skip its thinking pass (reasoning_effort: none)")
    p.add_argument("--allow-remote-model", action="store_true",
                   help="allow a model endpoint off this host or network (may cost money; off by default)")
    roots(p)
    p.set_defaults(func=cmd_eval)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def cmd_eval(args: argparse.Namespace) -> int:
    from archi.bundles import discovery
    from archi.evaluation.agent import ModelError, require_local
    from archi.evaluation.smoke import SmokeError, load_smoke, smoke_path
    from archi.stack.instance import Instance, InstanceError, read_env

    token = os.environ.get(TOKEN_VAR, "")
    bundles = list(args.bundle)
    mcp_url = args.mcp_url
    report_root = Path("eval-reports")
    chat_prompt_file = args.chat_prompt
    instance = None
    # With --mcp-url the endpoint is not the instance's own, so nothing is taken from an
    # instance: its bearer token must never be sent to another server.
    if not mcp_url:
        try:
            instance = Instance.load(args.instance)
        except InstanceError as exc:
            return _fail(str(exc))
    if instance is not None:
        # The data root lies outside the instance repository: `archi up` commits
        # everything inside it, and a report is not configuration.
        report_root = instance.data_root / "eval"
        if chat_prompt_file is None:
            found = instance.deployment_dir / "skills" / "chat-system-prompt.md"
            chat_prompt_file = found if found.is_file() else None
        bundles = bundles or list(instance.bundles)
        mcp_url = f"http://127.0.0.1:{instance.ports.mcp}/mcp"
        try:
            token = read_env(instance.env_path).get(TOKEN_VAR) or token
        except InstanceError as exc:
            if not token:
                return _fail(str(exc))
    if not bundles:
        return _fail("name a bundle with --bundle")
    if not token:
        return _fail(f"no MCP bearer token: {TOKEN_VAR} is not in the instance .env or the environment")

    catalog = discovery.discover(args.pack_root, args.bundle_root)
    plan = []
    for name in bundles:
        bundle = catalog.bundles.get(name)
        if bundle is None:
            return _fail(f"no bundle named {name!r}")
        path = smoke_path(bundle.dir)
        if not path.is_file():
            if args.bundle:
                return _fail(f"bundle {name!r} ships no smoke set ({path})")
            print(f"note: bundle {name!r} ships no smoke set; skipped", file=sys.stderr)
            continue
        try:
            plan.append((bundle, load_smoke(path)))
        except SmokeError as exc:
            return _fail(str(exc))
    if not plan:
        return _fail("none of the bundles ships a smoke set (bundles/<name>/eval/smoke.yaml)")

    if not args.allow_remote_model:
        try:
            require_local(args.model_url)
        except ModelError as exc:
            return _fail(str(exc))

    worst = 0
    for bundle, smoke in plan:
        model_name = args.model or ((bundle.manifest.get("chat") or {}).get("default_model"))
        if not model_name:
            return _fail(f"bundle {bundle.name!r} sets no chat.default_model; pass --model")
        out = args.out or report_root / bundle.name
        if args.out and len(plan) > 1:
            out = args.out / bundle.name
        code = _run_one(smoke, model_name, args, mcp_url, token, out, chat_prompt_file)
        worst = max(worst, code)
    return worst


def _run_one(smoke, model_name: str, args: argparse.Namespace, mcp_url: str, token: str, out: Path,
             chat_prompt_file=None) -> int:
    import asyncio

    from archi.evaluation.agent import McpToolBox, OpenAIChat
    from archi.evaluation.runner import run_smoke, write_report

    chat_prompt = None
    if chat_prompt_file is not None:
        try:
            chat_prompt = Path(chat_prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        print("note: no chat system prompt found for this instance; the agent gets only the MCP "
              "server's instructions", file=sys.stderr)
    model = OpenAIChat(args.model_url, model_name, no_think=args.no_think)

    def progress(result, i: int, total: int) -> None:
        print(f"[{i}/{total}] {result.status:16} {result.id} ({result.seconds:.0f}s)", flush=True)

    print(f"archi eval: {smoke.bundle}, {len(smoke.questions)} questions, model {model_name}, MCP {mcp_url}")
    report = asyncio.run(
        run_smoke(
            smoke,
            model,
            lambda: McpToolBox(mcp_url, token),
            model_url=args.model_url,
            mcp_url=mcp_url,
            chat_prompt=chat_prompt,
            chat_prompt_file=str(chat_prompt_file or ""),
            max_steps=args.max_steps,
            progress=progress,
        )
    )
    md, js = write_report(report, out)
    print(f"{report.status}: {report.passed}/{len(report.questions)} passed "
          f"(threshold {report.pass_threshold}); {md} and {js}")
    if report.error:
        print(f"error: {report.error}", file=sys.stderr)
    return report.exit_code
