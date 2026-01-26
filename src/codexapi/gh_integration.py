import logging
import re
import time
from pathlib import Path

from tqdm import tqdm

from gh_task.project import Project, UPDATE_STATUS_MUTATION

from .taskfile import TaskFile


_logger = logging.getLogger(__name__)

_PROGRESS_HEADER = "## Progress"
_SUCCESS_LABEL = "✓"
_FAILURE_LABEL = "⨉"
_SUCCESS_COLOR = "2da44e"
_FAILURE_COLOR = "d73a4a"
_OWNER_PREFIX = "owner:"


def _canonical_task_name(path):
    return Path(path).stem


def project_url(project):
    """Return a GitHub URL for the Project board."""
    owner = project.owner
    number = project.number
    try:
        owner_type = project._get_owner_type()
    except Exception:
        owner_type = None
    if owner_type == "organization":
        prefix = "orgs"
    elif owner_type == "user":
        prefix = "users"
    else:
        prefix = None
    if prefix:
        return f"https://github.com/{prefix}/{owner}/projects/{number}"
    return f"https://github.com/{owner}/projects/{number}"


def reset_project_tasks(project, name, description=False):
    """Reset owned issues in a project back to Ready."""
    project = Project(project, name)
    owner_projects = {}
    issues = []
    for status in project.statuses():
        for issue in project.list(status, return_issue=True):
            issue = project.get_issue(issue, require_project_item=True)
            labels = issue.labels or []
            owner_labels = [label for label in labels if label.lower().startswith(_OWNER_PREFIX)]
            if not owner_labels:
                continue
            issues.append((issue, owner_labels))

    ready_name, ready_option = project._resolve_status("Ready")
    project._ensure_project_loaded()
    try:
        project._resolve_number_field("Estimate")
        estimate_supported = True
    except Exception:
        estimate_supported = False

    for issue, owner_labels in issues:
        owner_name = None
        for label in owner_labels:
            parts = label.split(":", 1)
            if len(parts) == 2 and parts[1].strip():
                owner_name = parts[1].strip()
                break
        if owner_name and estimate_supported:
            owner_project = owner_projects.get(owner_name)
            if owner_project is None:
                owner_project = Project(project.owner + "/projects/" + str(project.number), owner_name)
                owner_projects[owner_name] = owner_project
            owner_project.set_estimate(issue, None)
        for label in owner_labels:
            project._remove_label(issue, label)
        project._remove_label(issue, _SUCCESS_LABEL)
        project._remove_label(issue, _FAILURE_LABEL)
        if (issue.status or "").lower() != ready_name.lower():
            project.client.graphql(
                UPDATE_STATUS_MUTATION,
                {
                    "projectId": project._project_id,
                    "itemId": issue.project_item_id,
                    "fieldId": project._status_field_id,
                    "optionId": ready_option,
                },
            )
        if description:
            body = issue.body if issue.body is not None else project.get_issue_body(issue)
            cleaned = _strip_progress_section(body)
            if cleaned != body:
                project.set_issue_body(issue, cleaned)
    return [issue for issue, _labels in issues]


def _task_file_map(task_files):
    mapping = {}
    for path in task_files:
        name = _canonical_task_name(path)
        if not name:
            raise ValueError(f"Task file name is empty: {path}")
        key = name.lower()
        if key in mapping:
            raise ValueError(f"Duplicate task name '{name}' for {path} and {mapping[key][1]}")
        mapping[key] = (name, path)
    if not mapping:
        raise ValueError("At least one task file is required")
    return mapping


def _issue_url(issue):
    if issue.url:
        return issue.url
    return f"https://github.com/{issue.repo}/issues/{issue.number}"


def _match_task_file(issue, task_map):
    labels = issue.labels or []
    matches = []
    for label in labels:
        key = label.strip().lower()
        if key in task_map:
            matches.append((label, task_map[key][1]))
    if not matches:
        raise ValueError(f"Issue {_issue_url(issue)} has no matching task label")
    if len(matches) > 1:
        details = ", ".join(f"{label} -> {path}" for label, path in matches)
        raise ValueError(
            f"Issue {_issue_url(issue)} matches multiple task labels: {details}"
        )
    return matches[0][1]


def _strip_progress_section(body):
    if not body:
        return ""
    match = re.search(r"(?m)^## Progress\s*$", body)
    if not match:
        return body.strip()
    return body[:match.start()].rstrip()


def _format_item_text(issue, description):
    title = issue.title or ""
    url = _issue_url(issue)
    description = description or ""
    return f"Issue: {url}\nTitle: {title}\nDescription: {description}\n"


def _format_status_line(status_line):
    match = re.match(r"^\[(?P<turns>[^ ]+) @ (?P<elapsed>[^\]]+)\]:\s*(?P<summary>.*)$", status_line)
    if not match:
        return status_line
    summary = match.group("summary").strip()
    prefix = f"`[{match.group('turns')} {match.group('elapsed')}]`"
    if summary:
        return f"{prefix} {summary}"
    return prefix


def _format_progress_bar(total, remaining, start_time):
    if total is None:
        total = 0
    current = total - remaining
    if current < 0:
        current = 0
    elapsed = 0.0
    if start_time is not None:
        elapsed = time.monotonic() - start_time
    total_for_bar = total if total > 0 else 1
    return tqdm.format_meter(current, total_for_bar, elapsed, ncols=80)


def _render_progress_section(base_body, status_line, bar_text):
    parts = [
        _PROGRESS_HEADER,
        "",
        status_line,
        "",
        "```",
        bar_text,
        "```",
    ]
    section = "\n".join(parts).rstrip()
    if base_body:
        return f"{base_body.rstrip()}\n\n{section}\n"
    return f"{section}\n"


class GhTaskFile(TaskFile):
    def __init__(
        self,
        path,
        issue,
        project,
        item_text,
        cwd=None,
        yolo=True,
        thread_id=None,
        flags=None,
    ):
        super().__init__(path, item_text, None, cwd, yolo, thread_id, flags)
        self.issue = issue
        self.project = project
        self._progress_updates = True

    def on_progress(
        self,
        iterations,
        max_iterations,
        total_estimate,
        remaining_estimate,
        status_line,
    ):
        super().on_progress(
            iterations,
            max_iterations,
            total_estimate,
            remaining_estimate,
            status_line,
        )
        try:
            self.project.set_estimate(self.issue, remaining_estimate)
        except Exception as exc:
            _logger.warning("Failed to update estimate for issue %s", _issue_url(self.issue), exc_info=exc)
        if not status_line:
            return
        try:
            body = self.project.get_issue_body(self.issue)
            base = _strip_progress_section(body)
            status = _format_status_line(status_line)
            bar_text = _format_progress_bar(total_estimate, remaining_estimate, self._progress_start)
            updated = _render_progress_section(base, status, bar_text)
            self.project.set_issue_body(self.issue, updated)
        except Exception as exc:
            _logger.warning("Failed to update issue progress for %s", _issue_url(self.issue), exc_info=exc)

    def on_success(self, result):
        super().on_success(result)
        self.project.ensure_label(
            self.issue.repo,
            _SUCCESS_LABEL,
            color=_SUCCESS_COLOR,
            description="Task succeeded",
        )
        self.project.add_label(self.issue, _SUCCESS_LABEL)

    def on_failure(self, result):
        super().on_failure(result)
        self.project.ensure_label(
            self.issue.repo,
            _FAILURE_LABEL,
            color=_FAILURE_COLOR,
            description="Task failed",
        )
        self.project.add_label(self.issue, _FAILURE_LABEL)

    def tear_down(self):
        super().tear_down()
        self.project.move(self.issue, "In review")
        self.project.release(self.issue)


class GhTaskRunner:
    def __init__(
        self,
        project,
        name,
        task_files,
        status="Ready",
        cwd=None,
        yolo=True,
        flags=None,
    ):
        task_map = _task_file_map(task_files)
        self.project = Project(project, name, has_label=list(task_map))
        self.issue = self.project.take(status=status, return_issue=True)
        self.issue = self.project.get_issue(self.issue)
        try:
            task_path = _match_task_file(self.issue, task_map)
        except Exception:
            self.project.release(self.issue)
            raise
        try:
            self.project.move(self.issue, "In progress")
        except Exception:
            self.project.release(self.issue)
            raise
        self.task_name = _canonical_task_name(task_path)
        self.issue_title = (self.issue.title or "").strip()
        body = self.project.get_issue_body(self.issue)
        description = _strip_progress_section(body)
        item_text = _format_item_text(self.issue, description)
        self.task = GhTaskFile(
            task_path,
            self.issue,
            self.project,
            item_text,
            cwd,
            yolo,
            None,
            flags,
        )

    def __call__(self, progress=False):
        return self.task(progress=progress)
