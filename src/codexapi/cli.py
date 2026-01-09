import argparse
import sys

from .agent import Agent, agent
from .task import TaskFailed, task


def _read_prompt(prompt):
    if prompt and prompt != "-":
        return prompt

    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("No prompt provided. Pass a prompt or pipe via stdin.")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="codexapi",
        description="Run Codex via the codexapi wrapper.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Prompt to send. Use '-' or omit to read from stdin.",
    )
    parser.add_argument(
        "--task",
        action="store_true",
        help="Run in task mode with verification retries.",
    )
    parser.add_argument(
        "--check",
        help="Optional check prompt for --task. Defaults to the task prompt.",
    )
    parser.add_argument("--cwd", help="Working directory for the Codex session.")
    parser.add_argument("--yolo", action="store_true", help="Pass --yolo to Codex.")
    parser.add_argument(
        "--flags",
        help="Additional raw CLI flags to pass to Codex (quoted as needed).",
    )
    parser.add_argument(
        "--thread-id",
        help="Resume an existing Codex thread id.",
    )
    parser.add_argument(
        "--print-thread-id",
        action="store_true",
        help="Print the current thread id to stderr after running.",
    )

    args = parser.parse_args(argv)
    if args.check is not None and not args.task:
        raise SystemExit("--check requires --task.")
    if args.task and (args.thread_id or args.print_thread_id):
        raise SystemExit("--thread-id/--print-thread-id are not supported with --task.")

    prompt = _read_prompt(args.prompt)

    exit_code = 0

    if args.task:
        check = args.check if args.check is not None else prompt
        try:
            message = task(
                prompt,
                check,
                cwd=args.cwd,
                yolo=args.yolo,
                flags=args.flags,
            )
        except TaskFailed as exc:
            message = exc.summary
            exit_code = 1
    else:
        use_session = args.thread_id or args.print_thread_id
        if use_session:
            session = Agent(
                args.cwd,
                args.yolo,
                args.thread_id,
                args.flags,
            )
            message = session(prompt)
            if args.print_thread_id:
                print(f"thread_id={session.thread_id}", file=sys.stderr)
        else:
            message = agent(prompt, args.cwd, args.yolo, args.flags)

    print(message)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
