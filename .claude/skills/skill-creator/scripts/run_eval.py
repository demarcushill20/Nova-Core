#!/usr/bin/env python3
"""Run trigger evaluation for a skill description.

Tests whether a skill's description causes Claude to trigger (read the skill)
for a set of queries. Outputs results as JSON.
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from scripts.utils import parse_skill_md


def find_project_root() -> Path:
    """Find the project root by walking up from cwd looking for .claude/.

    Mimics how Claude Code discovers its project root, so the command file
    we create ends up where claude -p will look for it.
    """
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".claude").is_dir():
            return parent
    return current


def _matches_skill(text: str, clean_name: str, skill_name: str) -> bool:
    """Check if text references the temp command alias OR the real installed skill.

    Matches the temp alias (e.g. "web-research-skill-a1b2c3d4") or the real
    skill name (e.g. "web-research") so that evaluation works correctly whether
    Claude routes to the temp command or the already-installed real skill.
    """
    return clean_name in text or skill_name in text


def _get_allowed_tools(skill_name: str, project_root: str) -> set[str]:
    """Extract allowed-tools from the real installed skill's SKILL.md.

    Returns an empty set if the skill is not installed or has no allowed-tools.
    Used to detect indirect activation where Claude loads the skill's tools
    via ToolSearch instead of routing through the Skill tool.
    """
    import re

    import yaml

    skill_md = Path(project_root) / ".claude" / "skills" / skill_name / "SKILL.md"
    if not skill_md.exists():
        return set()
    try:
        text = skill_md.read_text()
        fm_match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not fm_match:
            return set()
        fm = yaml.safe_load(fm_match.group(1))
        tools = fm.get("allowed-tools", [])
        return set(tools) if isinstance(tools, list) else set()
    except Exception:
        return set()


def run_single_query(
    query: str,
    skill_name: str,
    skill_description: str,
    timeout: int,
    project_root: str,
    model: str | None = None,
) -> bool:
    """Run a single query and return whether the skill was triggered.

    Creates a command file in .claude/commands/ so it appears in Claude's
    available_skills list, then runs `claude -p` with the raw query.
    Uses --include-partial-messages to detect triggering early from
    stream events (content_block_start) rather than waiting for the
    full assistant message, which only arrives after tool execution.

    Detects triggering via three paths:
    1. Claude invokes the Skill tool with the skill name
    2. Claude reads the skill's SKILL.md via the Read tool
    3. Claude loads one of the skill's allowed-tools via ToolSearch
       (indirect activation — Claude bypasses Skill routing but uses
       the same underlying tools the skill would use)
    """
    unique_id = uuid.uuid4().hex[:8]
    clean_name = f"{skill_name}-skill-{unique_id}"
    project_commands_dir = Path(project_root) / ".claude" / "commands"
    command_file = project_commands_dir / f"{clean_name}.md"

    # Build the path pattern for the real installed skill's SKILL.md so we
    # can detect Read tool calls that target it directly.
    real_skill_path_fragment = f".claude/skills/{skill_name}"

    # Get the skill's allowed-tools for indirect activation detection.
    allowed_tools = _get_allowed_tools(skill_name, project_root)

    try:
        project_commands_dir.mkdir(parents=True, exist_ok=True)
        # Use YAML block scalar to avoid breaking on quotes in description
        indented_desc = "\n  ".join(skill_description.split("\n"))
        command_content = (
            f"---\n"
            f"description: |\n"
            f"  {indented_desc}\n"
            f"---\n\n"
            f"# {skill_name}\n\n"
            f"This skill handles: {skill_description}\n"
        )
        command_file.write_text(command_content)

        cmd = [
            "claude",
            "-p",
            query,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if model:
            cmd.extend(["--model", model])

        # Remove CLAUDECODE env var to allow nesting claude -p inside a
        # Claude Code session. The guard is for interactive terminal conflicts;
        # programmatic subprocess usage is safe.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=project_root,
            env=env,
        )

        triggered = False
        start_time = time.time()
        buffer = ""
        # Track state for stream event detection
        pending_tool_name = None
        accumulated_json = ""

        try:
            while time.time() - start_time < timeout:
                if process.poll() is not None:
                    remaining = process.stdout.read()
                    if remaining:
                        buffer += remaining.decode("utf-8", errors="replace")
                    break

                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue

                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Early detection via stream events
                    if event.get("type") == "stream_event":
                        se = event.get("event", {})
                        se_type = se.get("type", "")

                        if se_type == "content_block_start":
                            cb = se.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                tool_name = cb.get("name", "")
                                if tool_name in ("Skill", "Read", "ToolSearch"):
                                    pending_tool_name = tool_name
                                    accumulated_json = ""
                                elif allowed_tools and tool_name in allowed_tools:
                                    # Claude is directly calling one of the
                                    # skill's allowed tools (e.g. tavily_search)
                                    # without going through Skill or ToolSearch.
                                    return True
                                else:
                                    # Non-skill-related tool call. Continue
                                    # scanning — don't short-circuit.
                                    continue

                        elif se_type == "content_block_delta" and pending_tool_name:
                            delta = se.get("delta", {})
                            if delta.get("type") == "input_json_delta":
                                accumulated_json += delta.get("partial_json", "")
                                if pending_tool_name in ("Skill", "Read"):
                                    if _matches_skill(accumulated_json, clean_name, skill_name):
                                        return True
                                elif pending_tool_name == "ToolSearch" and allowed_tools:
                                    # Check if ToolSearch is loading one of
                                    # the skill's allowed tools.
                                    for tool in allowed_tools:
                                        if tool in accumulated_json:
                                            return True

                        elif se_type in ("content_block_stop", "message_stop"):
                            if pending_tool_name:
                                if pending_tool_name in ("Skill", "Read"):
                                    if _matches_skill(accumulated_json, clean_name, skill_name):
                                        return True
                                elif pending_tool_name == "ToolSearch" and allowed_tools:
                                    for tool in allowed_tools:
                                        if tool in accumulated_json:
                                            return True
                                # Reset for next content block (Claude may
                                # emit multiple tool calls in one message).
                                pending_tool_name = None
                                accumulated_json = ""
                                continue
                            if se_type == "message_stop":
                                return triggered

                    # Fallback: full assistant message
                    elif event.get("type") == "assistant":
                        message = event.get("message", {})
                        tool_uses = [ci for ci in message.get("content", []) if ci.get("type") == "tool_use"]
                        if not tool_uses:
                            # Assistant message with no tool calls (e.g.
                            # thinking-only or text-only partial message).
                            # Skip — don't return early; more events may
                            # follow with actual tool invocations.
                            continue
                        for content_item in tool_uses:
                            tool_name = content_item.get("name", "")
                            tool_input = content_item.get("input", {})
                            if (
                                tool_name == "Skill"
                                and _matches_skill(tool_input.get("skill", ""), clean_name, skill_name)
                            ) or (
                                tool_name == "Read"
                                and (
                                    _matches_skill(tool_input.get("file_path", ""), clean_name, skill_name)
                                    or real_skill_path_fragment in tool_input.get("file_path", "")
                                )
                            ):
                                triggered = True
                            elif tool_name == "ToolSearch" and allowed_tools:
                                q = tool_input.get("query", "")
                                for tool in allowed_tools:
                                    if tool in q:
                                        triggered = True
                                        break
                            elif allowed_tools and tool_name in allowed_tools:
                                # Direct call to an allowed tool
                                triggered = True
                        if triggered:
                            return True
                        # Don't return False yet — more messages may follow
                        continue

                    elif event.get("type") == "result":
                        return triggered
        finally:
            # Clean up process on any exit path (return, exception, timeout)
            if process.poll() is None:
                process.kill()
                process.wait()

        return triggered
    finally:
        if command_file.exists():
            command_file.unlink()


def run_eval(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    num_workers: int,
    timeout: int,
    project_root: Path,
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
    model: str | None = None,
) -> dict:
    """Run the full eval set and return results."""
    results = []

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        future_to_info = {}
        for item in eval_set:
            for run_idx in range(runs_per_query):
                future = executor.submit(
                    run_single_query,
                    item["query"],
                    skill_name,
                    description,
                    timeout,
                    str(project_root),
                    model,
                )
                future_to_info[future] = (item, run_idx)

        query_triggers: dict[str, list[bool]] = {}
        query_items: dict[str, dict] = {}
        for future in as_completed(future_to_info):
            item, _ = future_to_info[future]
            query = item["query"]
            query_items[query] = item
            if query not in query_triggers:
                query_triggers[query] = []
            try:
                query_triggers[query].append(future.result())
            except Exception as e:
                print(f"Warning: query failed: {e}", file=sys.stderr)
                query_triggers[query].append(False)

    for query, triggers in query_triggers.items():
        item = query_items[query]
        trigger_rate = sum(triggers) / len(triggers)
        should_trigger = item["should_trigger"]
        if should_trigger:
            did_pass = trigger_rate >= trigger_threshold
        else:
            did_pass = trigger_rate < trigger_threshold
        results.append(
            {
                "query": query,
                "should_trigger": should_trigger,
                "trigger_rate": trigger_rate,
                "triggers": sum(triggers),
                "runs": len(triggers),
                "pass": did_pass,
            }
        )

    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    return {
        "skill_name": skill_name,
        "description": description,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Run trigger evaluation for a skill description")
    parser.add_argument("--eval-set", required=True, help="Path to eval set JSON file")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--description", default=None, help="Override description to test")
    parser.add_argument("--num-workers", type=int, default=10, help="Number of parallel workers")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout per query in seconds")
    parser.add_argument("--runs-per-query", type=int, default=3, help="Number of runs per query")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="Trigger rate threshold")
    parser.add_argument("--model", default=None, help="Model to use for claude -p (default: user's configured model)")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, original_description, content = parse_skill_md(skill_path)
    description = args.description or original_description
    project_root = find_project_root()

    if args.verbose:
        print(f"Evaluating: {description}", file=sys.stderr)

    output = run_eval(
        eval_set=eval_set,
        skill_name=name,
        description=description,
        num_workers=args.num_workers,
        timeout=args.timeout,
        project_root=project_root,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        model=args.model,
    )

    if args.verbose:
        summary = output["summary"]
        print(f"Results: {summary['passed']}/{summary['total']} passed", file=sys.stderr)
        for r in output["results"]:
            status = "PASS" if r["pass"] else "FAIL"
            rate_str = f"{r['triggers']}/{r['runs']}"
            print(f"  [{status}] rate={rate_str} expected={r['should_trigger']}: {r['query'][:70]}", file=sys.stderr)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
