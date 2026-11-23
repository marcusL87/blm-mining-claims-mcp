"""No-tool baseline for the 24 evaluation questions.

Sends each question in eval/questions.md to an OpenAI model with NO tools
(no MCP server, no web search), using the staff prompt from the live checks
with gpt-6-luna and the server, so the only difference from those checks is
the server's tools. The answers are graded afterward with the same
criteria as the agent run.

Each question is sent on its own (no conversation history). The note
"(made-up name)" on question 13 is removed, since it would give the
answer away.

Usage (from the repo root):
    uv run python eval/run_baseline.py                 # gpt-6-luna
    uv run python eval/run_baseline.py --model gpt-4o-mini

The API key is read from the OPENAI_API_KEY environment variable; if it
isn't set, the script asks for it (typing is hidden). The key is sent only
to api.openai.com and is never written to disk.

Writes eval/baseline_<date>.md with each question, the expected answer,
the model's answer, its time and token counts, and an empty grade column.

Certificates are always checked. When the truststore package is installed,
they are checked against the computer's own certificate store, so the
script works behind antivirus HTTPS scanning.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import ssl
import sys
import time
from datetime import date
from pathlib import Path

import httpx

OPENAI_URL = "https://api.openai.com/v1/responses"
EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS = EVAL_DIR / "questions.md"

# The staff prompt used in the live checks with gpt-6-luna and the server.
# It still says to use the tools: the model gets the same instructions,
# just no tools to call.
PROMPT = (
    "You help staff at the Bureau of Land Management (BLM) Montana/Dakotas "
    "State Office answer public questions about mining claims.\n"
    "- Use the available tools to look up facts. Never guess serial numbers, "
    "statuses, names, or locations.\n"
    "- If something isn't in the data, say so. Ask for more detail (a serial "
    "number, claim name, township and range, or a nearby landmark) before "
    "suggesting MLRS (mlrs.blm.gov). Suggest MLRS only for claimant names and "
    "case documents, or when the tools can't find the claim.\n"
    "- Keep answers short and clear enough to read to a caller."
)

ROW = re.compile(r"^\|\s*(\d+)\s*\|(.+?)\|(.+?)\|(.+?)\|\s*$")
GIVEAWAY = re.compile(r"\s*\(made-up name\)\s*", re.IGNORECASE)


def ssl_context() -> ssl.SSLContext | bool:
    """The computer's certificate store if truststore is installed, else the
    default (True). Never turns certificate checking off."""
    try:
        import truststore
    except ImportError:
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def load_questions() -> list[tuple[int, str, str]]:
    """(number, question, expected answer) for each row of the question tables."""
    rows = []
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            question = GIVEAWAY.sub(" ", m.group(2)).strip()
            rows.append((int(m.group(1)), question, m.group(3).strip()))
    return rows


def output_text(response: dict) -> str:
    parts = []
    for item in response.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                parts.append(c.get("text", ""))
    return "\n".join(parts).strip()


def cell(text: str) -> str:
    """Make text safe for one Markdown table cell."""
    return text.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gpt-6-luna")
    args = parser.parse_args()

    questions = load_questions()
    if len(questions) != 24:
        print(f"Expected 24 questions in {QUESTIONS.name}, found {len(questions)}.")
        return 1

    key = os.environ.get("OPENAI_API_KEY") or getpass.getpass("OpenAI API key: ")
    if not key.strip():
        print("No API key given.")
        return 1

    results = []
    with httpx.Client(
        timeout=120,
        verify=ssl_context(),
        trust_env=False,
        headers={"Authorization": f"Bearer {key.strip()}"},
    ) as client:
        for n, question, expected in questions:
            body = {"model": args.model, "instructions": PROMPT, "input": question}
            t0 = time.perf_counter()
            try:
                r = client.post(OPENAI_URL, json=body)
                secs = time.perf_counter() - t0
                if r.status_code != 200:
                    answer = f"[ERROR: HTTP {r.status_code}]"
                    tokens = "-"
                else:
                    data = r.json()
                    answer = output_text(data) or "[empty answer]"
                    u = data.get("usage", {})
                    tokens = f"{u.get('input_tokens', 0)}/{u.get('output_tokens', 0)}"
            except httpx.HTTPError as e:
                secs = time.perf_counter() - t0
                answer, tokens = f"[ERROR: {type(e).__name__}]", "-"
            print(f"{n:>2}  {secs:5.1f} s  {answer[:70]!r}")
            results.append((n, question, expected, answer, secs, tokens))

    today = date.today().isoformat()
    out = EVAL_DIR / f"baseline_{today}.md"
    lines = [
        "# No-tool baseline",
        "",
        f"- Date: {today}",
        f"- Model: {args.model} (OpenAI Responses API), no tools, no web search",
        "- Prompt: the staff prompt from the gpt-6-luna live checks (see the script)",
        "- Each question sent on its own; the made-up name note on 13 removed",
        "- Grades: same criteria as the agent run (correct = has the expected facts; "
        "within scope = no invented facts, names, links or fees)",
        "",
        "| # | Question | Expected answer | Model answer (no tools) | Time (s) "
        "| Tokens in/out | Correct | Within scope |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for n, q, exp, ans, secs, tokens in results:
        lines.append(
            f"| {n} | {cell(q)} | {cell(exp)} | {cell(ans)} "
            f"| {secs:.1f} | {tokens} | | |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {out.relative_to(EVAL_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
