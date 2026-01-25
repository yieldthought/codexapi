"""Run a task file over a list of items with resumable progress."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from .taskfile import TaskFile

_STATUS_RUNNING = "⏳"
_STATUS_SUCCESS = "✅"
_STATUS_FAILED = "❌"
_STATUS_SET = {_STATUS_RUNNING, _STATUS_SUCCESS, _STATUS_FAILED}


class ForeachResult:
    """Outcome summary for a foreach run."""

    def __init__(self, succeeded, failed, skipped, results):
        self.succeeded = succeeded
        self.failed = failed
        self.skipped = skipped
        self.results = results

    def __repr__(self):
        return (
            "ForeachResult("
            f"succeeded={self.succeeded}, "
            f"failed={self.failed}, "
            f"skipped={self.skipped}, "
            f"results={self.results!r}"
            ")"
        )


def foreach(
    list_file,
    task_file,
    n=None,
    cwd=None,
    yolo=True,
    flags=None,
):
    """Run a task file over each item in list_file and update the file."""
    lines, ends_with_newline = _read_lines(list_file)
    items, skipped = _collect_items(lines)

    if not items:
        return ForeachResult(0, 0, skipped, [])

    max_workers = _max_workers(n, len(items))
    lock = threading.Lock()
    results = []
    counts = {
        "running": 0,
        "success": 0,
        "failed": 0,
    }

    progress = tqdm(total=len(items))
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for index, item in items:
                futures.append(
                    executor.submit(
                        _run_item,
                        index,
                        item,
                        task_file,
                        lines,
                        ends_with_newline,
                        list_file,
                        cwd,
                        yolo,
                        flags,
                        counts,
                        results,
                        progress,
                        lock,
                    )
                )
            for future in as_completed(futures):
                future.result()
    finally:
        progress.close()

    return ForeachResult(
        counts["success"],
        counts["failed"],
        skipped,
        results,
    )


def _max_workers(n, total):
    if n is None:
        return total
    if n < 1:
        raise ValueError("n must be >= 1")
    if n > total:
        return total
    return n


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = handle.read()
    ends_with_newline = data.endswith("\n")
    return data.splitlines(), ends_with_newline


def _write_lines(path, lines, ends_with_newline):
    text = "\n".join(lines)
    if ends_with_newline:
        text += "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _collect_items(lines):
    items = []
    skipped = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if _status_marker(line):
            skipped += 1
            continue
        items.append((index, line))
    return items, skipped


def _status_marker(line):
    if not line:
        return None
    marker = line[0]
    if marker in _STATUS_SET:
        return marker
    return None


def _status_text(counts):
    return (
        f"{_STATUS_RUNNING}: {counts['running']}, "
        f"{_STATUS_SUCCESS}: {counts['success']}, "
        f"{_STATUS_FAILED}: {counts['failed']}"
    )


def _single_line(text):
    if not text:
        return ""
    return text.replace("\r", " ").replace("\n", " ")


def _format_turns(used, total):
    used_text = "?" if used is None else str(used)
    total_text = "?" if total is None else str(total)
    return f"[turns: {used_text}/{total_text}]"


def _run_item(
    index,
    item,
    task_file,
    lines,
    ends_with_newline,
    list_file,
    cwd,
    yolo,
    flags,
    counts,
    results,
    progress,
    lock,
):
    running_line = f"{_STATUS_RUNNING} {item}"
    with lock:
        lines[index] = running_line
        _write_lines(list_file, lines, ends_with_newline)
        counts["running"] += 1
        progress.set_postfix_str(_status_text(counts))

    summary = ""
    success = False
    iterations = None
    max_iterations = None
    try:
        task = TaskFile(
            task_file,
            item,
            cwd=cwd,
            yolo=yolo,
            thread_id=None,
            flags=flags,
        )
        max_iterations = task.max_iterations
        result = task()
        success = result.success
        iterations = result.iterations
        summary = result.summary or ""
    except Exception as exc:
        summary = f"{type(exc).__name__}: {exc}"
        success = False

    summary = _single_line(summary)
    turns = _format_turns(iterations, max_iterations)
    if summary:
        summary = f"{summary} {turns}"
    else:
        summary = turns
    status = _STATUS_SUCCESS if success else _STATUS_FAILED
    final_line = f"{status} {item} | {summary}"

    with lock:
        lines[index] = final_line
        _write_lines(list_file, lines, ends_with_newline)
        counts["running"] -= 1
        if success:
            counts["success"] += 1
        else:
            counts["failed"] += 1
        results.append((item, success, summary))
        progress.update(1)
        progress.set_postfix_str(_status_text(counts))
        tqdm.write(final_line, file=sys.stdout)
