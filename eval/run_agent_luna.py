"""Agent run with the MCP server: the 24 evaluation questions, gpt-6-luna.

The same-model counterpart of eval/run_baseline.py. Each question in
eval/questions.md goes to an OpenAI model with the same staff prompt as
the baseline, plus the server's own instructions and its five tools,
just as the browser test client sends them. So the only difference from
the baseline is the server.

The model never sees the expected answers (a blind run). Each question is
sent on its own (no conversation history), and the note "(made-up name)"
on question 13 is removed, as in the baseline.

The server runs in this process through FastMCP's in-memory client (the
same code path an MCP client uses; no separate server window). Its tools
call the LIVE public BLM and USGS services, one call at a time, to stay
light on BLM's shared service.

Usage (from the repo root):
    uv run python eval/run_agent_luna.py                 # gpt-6-luna
    uv run python eval/run_agent_luna.py --model gpt-4o-mini
    uv run python eval/run_agent_luna.py --questions 8,17,18,24   # rerun some

The API key is read from the OPENAI_API_KEY environment variable; if it
isn't set, the script asks for it (typing is hidden). The key is sent only
to api.openai.com and is never written to disk.

Writes eval/agent_luna_<date>.md with each question, the expected answer,
the model's answer, each tool call with its arguments, its time and token
counts, and empty grade columns.

Certificates are always checked. When the truststore package is installed,
they are checked against the computer's own certificate store, so the
script works behind antivirus HTTPS scanning.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from run_baseline import (  # noqa: E402
    EVAL_DIR,
    OPENAI_URL,
    PROMPT,
    cell,
    load_questions,
    output_text,
    ssl_context,
)

from mining_claims_mcp.app import mcp  # noqa: E402

MAX_ROUNDS = 12


def full_instructions(server_instructions: str) -> str:
    """The staff prompt plus the server's instructions, as the browser test
    client (a development aid, not in this repository) sends them."""
    text = server_instructions.strip()
    return f"{PROMPT}\n\nServer instructions:\n{text}" if text else PROMPT


def openai_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """The server's tools as OpenAI function tools (openaiTools() in the
    browser test client)."""
    out = []
    for t in tools:
        params = dict(t.input_schema or {})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        out.append(
            {
                "type": "function",
                "name": t.name,
                "description": t.description or "",
                "parameters": params,
                "strict": False,
            }
        )
    return out


def tool_result_text(reply: Any) -> str:
    """The text the model gets back from a tool call."""
    parts = [c.text for c in reply.content if getattr(c, "type", "") == "text"]
    if parts:
        return "\n".join(parts)
    return json.dumps(reply.structured_content)


async def run_tool(mcp_client: Client, call: dict[str, Any]) -> tuple[str, dict]:
    """Run one function call from the model; return (output text, record)."""
    record: dict[str, Any] = {
        "name": call.get("name"),
        "arguments": call.get("arguments"),
    }
    try:
        args = json.loads(call.get("arguments") or "{}")
    except json.JSONDecodeError:
        record["error"] = "arguments not valid JSON"
        return json.dumps(
            {"error": "The model sent arguments that aren't valid JSON."}
        ), record
    t0 = time.perf_counter()
    try:
        reply = await mcp_client.call_tool(call["name"], args, raise_on_error=False)
        text = tool_result_text(reply)
        if reply.is_error:
            record["error"] = text[:200]
    except Exception as e:  # unknown tool, bad arguments, and so on
        text = json.dumps({"error": f"{type(e).__name__}: {e}"})
        record["error"] = text[:200]
    record["seconds"] = round(time.perf_counter() - t0, 1)
    return text, record


async def ask(
    http: httpx.Client,
    mcp_client: Client,
    model: str,
    instructions: str,
    tools: list[dict[str, Any]],
    question: str,
) -> dict[str, Any]:
    """One question, with as many tool rounds as the model needs (up to 12)."""
    answer_parts: list[str] = []
    calls: list[dict] = []
    tokens_in = tokens_out = 0
    note = ""
    prev = None
    inputs: list[dict] = [{"role": "user", "content": question}]
    for round_no in range(1, MAX_ROUNDS + 1):
        body: dict[str, Any] = {
            "model": model,
            "input": inputs,
            "tools": tools,
            "instructions": instructions,
        }
        if prev:
            body["previous_response_id"] = prev
        # The HTTP call blocks; run it in a thread so the server's event loop
        # stays free.
        r = await asyncio.to_thread(http.post, OPENAI_URL, json=body)
        if r.status_code != 200:
            note = f"[ERROR: HTTP {r.status_code}]"
            break
        data = r.json()
        prev = data.get("id")
        u = data.get("usage", {})
        tokens_in += u.get("input_tokens", 0)
        tokens_out += u.get("output_tokens", 0)
        text = output_text(data)
        if text:
            answer_parts.append(text)
        fcalls = [o for o in data.get("output", []) if o.get("type") == "function_call"]
        if not fcalls:
            break
        if round_no == MAX_ROUNDS:
            note = f"[Stopped after {MAX_ROUNDS} tool rounds]"
            break
        inputs = []
        for fc in fcalls:  # one at a time, in order
            out, rec = await run_tool(mcp_client, fc)
            calls.append(rec)
            inputs.append(
                {
                    "type": "function_call_output",
                    "call_id": fc["call_id"],
                    "output": out,
                }
            )
    answer = "\n\n".join(answer_parts) or "[empty answer]"
    if note:
        answer = f"{answer}\n\n{note}"
    return {
        "answer": answer,
        "calls": calls,
        "tokens": f"{tokens_in}/{tokens_out}",
    }


def calls_cell(calls: list[dict]) -> str:
    if not calls:
        return "(none)"
    lines = []
    for i, c in enumerate(calls, 1):
        name = (c.get("name") or "?").removeprefix("mining_claims_")
        line = f"{i}. `{name}` {c.get('arguments')} ({c.get('seconds', 0)} s)"
        if c.get("error"):
            line += f" ERROR: {c['error']}"
        lines.append(line)
    return cell("\n".join(lines))


async def run(model: str, key: str, only: set[int] | None = None) -> int:
    questions = load_questions()
    if len(questions) != 24:
        print(f"Expected 24 questions, found {len(questions)}.")
        return 1
    if only:
        unknown = only - {n for n, _, _ in questions}
        if unknown:
            print(f"No such questions: {sorted(unknown)}")
            return 1
        questions = [q for q in questions if q[0] in only]

    results = []
    async with Client(mcp) as mcp_client:
        init = mcp_client.initialize_result
        instructions = full_instructions((init.instructions if init else "") or "")
        tools = openai_tools(await mcp_client.list_tools())
        print(f"{len(tools)} tools: {', '.join(t['name'] for t in tools)}")
        with httpx.Client(
            timeout=180,
            verify=ssl_context(),
            trust_env=False,
            headers={"Authorization": f"Bearer {key}"},
        ) as http:
            for n, question, expected in questions:
                t0 = time.perf_counter()
                try:
                    res = await ask(
                        http, mcp_client, model, instructions, tools, question
                    )
                except httpx.HTTPError as e:
                    res = {
                        "answer": f"[ERROR: {type(e).__name__}]",
                        "calls": [],
                        "tokens": "-",
                    }
                secs = time.perf_counter() - t0
                names = [
                    str(c.get("name", "")).removeprefix("mining_claims_")
                    for c in res["calls"]
                ]
                print(
                    f"{n:>2}  {secs:5.1f} s  {len(names)} calls {names}  "
                    f"{res['answer'][:60]!r}"
                )
                results.append((n, question, expected, res, secs))

    today = date.today().isoformat()
    # A partial run (--questions) gets its own file, so it never overwrites
    # the full run.
    suffix = ("_q" + "-".join(str(n) for n in sorted(only))) if only else ""
    out = EVAL_DIR / f"agent_luna_{today}{suffix}.md"
    title = "# Agent run with the server: gpt-6-luna"
    if only:
        title += f" (rerun of questions {', '.join(str(n) for n in sorted(only))})"
    lines = [
        title,
        "",
        f"- Date: {today}",
        f"- Model: {model} (OpenAI Responses API) with the MCP server's "
        f"{len(tools)} tools "
        "(in-memory FastMCP client, live BLM and USGS services); no web search",
        "- Prompt: the baseline's staff prompt plus the server's instructions, "
        "as the browser test client sends them (see `run_agent_luna.py`)",
        "- Blind run: the model never sees the expected answers. Each question sent "
        "on its own; the made-up name note on 13 removed",
        f"- Up to {MAX_ROUNDS} tool rounds per question; tool calls run one at a time",
        "- Grades: same criteria as the baseline (correct = has the expected facts, "
        "partial counts as not correct; within scope = no invented facts, names, "
        "links or fees)",
        "",
        "| # | Question | Expected answer | Model answer (with the server) "
        "| Tool calls "
        "| Time (s) | Tokens in/out | Correct | Within scope | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for n, q, exp, res, secs in results:
        lines.append(
            f"| {n} | {cell(q)} | {cell(exp)} | {cell(res['answer'])} "
            f"| {calls_cell(res['calls'])} | {secs:.1f} | {res['tokens']} | | | |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {out.relative_to(EVAL_DIR.parent)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument(
        "--questions",
        help="run only these question numbers, e.g. 8,17,18,24 "
        "(writes agent_luna_<date>_q8-17-18-24.md)",
    )
    args = parser.parse_args()
    only = None
    if args.questions:
        try:
            only = {int(x) for x in args.questions.split(",") if x.strip()}
        except ValueError:
            print("--questions takes numbers separated by commas, e.g. 8,17,18,24")
            return 1
    key = os.environ.get("OPENAI_API_KEY") or getpass.getpass("OpenAI API key: ")
    if not key.strip():
        print("No API key given.")
        return 1
    return asyncio.run(run(args.model, key.strip(), only))


if __name__ == "__main__":
    sys.exit(main())
