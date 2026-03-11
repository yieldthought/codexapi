"""Discord bridge for durable agents."""

import json
import re
import shutil
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .agents import _read_json, _write_json, codexapi_home, current_hostname

_API_BASE = "https://discord.com/api/v10"
_BOOK_LIMIT = 4000
_CHANNEL_LIMIT = 100
_CHANNEL_NAME_LIMIT = 100
_CHANNEL_TOPIC_LIMIT = 1024
_STATUS_EMOJI = {
    "running": "🤖",
    "ready": "💤",
    "paused": "✋",
    "done": "✅",
    "canceled": "❎",
    "error": "❌",
}
_HELP_TEXT = (
    "Commands: !wake, !set-heartbeat <minutes>, !pause, !resume, !cancel, "
    "!delete confirm, !status, !book, !help"
)
_TYPE_ROLE = 0
_TYPE_TEXT_CHANNEL = 0
_TYPE_MEMBER = 1
_TYPE_CATEGORY = 4
_PERM_MANAGE_CHANNELS = 1 << 4
_PERM_VIEW_CHANNEL = 1 << 10
_PERM_SEND_MESSAGES = 1 << 11
_PERM_READ_MESSAGE_HISTORY = 1 << 16
_USER_PERMISSIONS = _PERM_VIEW_CHANNEL | _PERM_SEND_MESSAGES | _PERM_READ_MESSAGE_HISTORY
_BOT_PERMISSIONS = _USER_PERMISSIONS | _PERM_MANAGE_CHANNELS


def discord_dir(home=None):
    """Return the durable Discord bridge directory."""
    return _resolve_home(home) / "discord"


def discord_enabled(home=None):
    """Return whether Discord bridging is enabled."""
    return bool(read_discord_config(home).get("enabled"))


def read_discord_config(home=None):
    """Read the current Discord bridge config."""
    path = _discord_config_path(home)
    if not path.exists():
        return {}
    return _read_json(path)


def discord_status(home=None):
    """Return a short status summary for Discord bridging."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    bridges = list_discord_bridges(home)
    return {
        "enabled": bool(config.get("enabled")),
        "bot_user_id": config.get("bot_user_id") or "",
        "bot_username": config.get("bot_username") or "",
        "application_id": config.get("application_id") or "",
        "guild_id": config.get("guild_id") or "",
        "guild_name": config.get("guild_name") or "",
        "user_id": config.get("user_id") or "",
        "category_id": config.get("category_id") or "",
        "owner_hostname": config.get("owner_hostname") or "",
        "bridge_count": len(bridges),
    }


def setup_discord(bot_token, guild_id, user_id, category_id=None, owner_hostname=None, home=None):
    """Configure Discord bridging for this codexapi home."""
    bot_token = str(bot_token or "").strip()
    guild_id = str(guild_id or "").strip()
    user_id = str(user_id or "").strip()
    category_id = str(category_id or "").strip()
    if not bot_token:
        raise ValueError("bot_token is required")
    if not guild_id:
        raise ValueError("guild_id is required")
    if not user_id:
        raise ValueError("user_id is required")
    home = _resolve_home(home)
    owner_hostname = str(owner_hostname or current_hostname()).strip() or current_hostname()

    probe = {
        "bot_token": bot_token,
        "guild_id": guild_id,
        "user_id": user_id,
        "category_id": category_id,
        "owner_hostname": owner_hostname,
    }
    bot = _discord_request(probe, "GET", "/users/@me")
    guild = _discord_request(probe, "GET", f"/guilds/{guild_id}")
    application = {}
    try:
        application = _discord_request(probe, "GET", "/oauth2/applications/@me")
    except ValueError:
        application = {}
    if category_id:
        category = _discord_request(probe, "GET", f"/channels/{category_id}")
        if int(category.get("type") or -1) != _TYPE_CATEGORY:
            raise ValueError("category_id does not refer to a category channel")
    config = {
        "enabled": True,
        "bot_token": bot_token,
        "bot_user_id": str(bot.get("id") or "").strip(),
        "bot_username": _user_name(bot),
        "application_id": str(application.get("id") or "").strip(),
        "guild_id": guild_id,
        "guild_name": str(guild.get("name") or "").strip(),
        "user_id": user_id,
        "category_id": category_id,
        "owner_hostname": owner_hostname,
    }
    _ensure_discord_dirs(home)
    _write_discord_config(config, home)
    backfilled = backfill_discord_bridges(home)
    result = discord_status(home)
    result["backfilled"] = backfilled["created"]
    result["deferred"] = backfilled["deferred"]
    return result


def uninstall_discord(home=None, delete_channels=False):
    """Disable Discord bridging and optionally delete bridged channels."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    bridges = list_discord_bridges(home)
    deleted = 0
    if config and delete_channels:
        for bridge in bridges:
            channel_id = bridge.get("channel_id") or ""
            if not channel_id:
                continue
            _discord_request(config, "DELETE", f"/channels/{channel_id}")
            deleted += 1
    shutil.rmtree(discord_dir(home), ignore_errors=True)
    return {
        "enabled": False,
        "deleted_channels": deleted,
        "removed": True,
    }


def list_discord_bridges(home=None):
    """Return all current Discord bridge records."""
    root = _discord_bridges_dir(home)
    if not root.exists():
        return []
    bridges = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix != ".json":
            continue
        bridges.append(_read_json(path))
    return bridges


def backfill_discord_bridges(home=None):
    """Create bridge records for all existing agents in this CODEXAPI_HOME."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    if not config.get("enabled"):
        return {"created": 0, "deferred": 0}
    root = home / "agents"
    if not root.exists():
        return {"created": 0, "deferred": 0}
    created = 0
    deferred = 0
    for agent_dir in sorted(root.iterdir(), key=lambda item: item.name):
        if not agent_dir.is_dir():
            continue
        meta_path = agent_dir / "meta.json"
        if not meta_path.exists():
            continue
        agent_meta = _read_json(meta_path)
        if read_discord_bridge(agent_meta["id"], home):
            continue
        bridge = ensure_discord_bridge(agent_meta, home)
        if bridge is None:
            continue
        if bridge.get("channel_id"):
            created += 1
        else:
            deferred += 1
    return {"created": created, "deferred": deferred}


def ensure_discord_bridge(agent_meta, home=None):
    """Create a Discord channel for one agent when Discord is enabled."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    if not config.get("enabled"):
        return None
    agent_id = agent_meta["id"]
    existing = read_discord_bridge(agent_id, home)
    if existing:
        return existing
    channel_name = _desired_channel_name(agent_meta, home)
    if config.get("owner_hostname") != current_hostname():
        bridge = {
            "agent_id": agent_id,
            "channel_id": "",
            "channel_name": channel_name,
            "last_message_id": "",
            "pending_user_turn": [],
            "last_sent_run_id": "",
            "active": True,
        }
        _write_discord_bridge(agent_id, bridge, home)
        return bridge
    return _create_discord_bridge(agent_meta, config, home)


def sync_discord_bridges(home=None, hostname=None):
    """Create missing Discord channels and reconcile channel names on the owner host."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    host = hostname or current_hostname()
    if not config.get("enabled") or config.get("owner_hostname") != host:
        return {"ran": False, "created": 0, "renamed": 0}
    created = 0
    renamed = 0
    for bridge in list_discord_bridges(home):
        if not bridge.get("active", True):
            continue
        if not bridge.get("channel_id"):
            agent_meta = _read_json(home / "agents" / bridge["agent_id"] / "meta.json")
            updated = _create_discord_bridge(agent_meta, config, home)
            if updated.get("channel_id"):
                created += 1
            continue
        if _sync_channel_name(config, bridge, home):
            renamed += 1
    return {"ran": True, "created": created, "renamed": renamed}


def sync_discord_channel_name(agent_id, home=None, hostname=None):
    """Rename one Discord channel when its mapped agent status changed."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    host = hostname or current_hostname()
    if not config.get("enabled") or config.get("owner_hostname") != host:
        return {"ran": False, "renamed": 0}
    bridge = read_discord_bridge(agent_id, home)
    if bridge is None or not bridge.get("active", True) or not bridge.get("channel_id"):
        return {"ran": True, "renamed": 0}
    renamed = 1 if _sync_channel_name(config, bridge, home) else 0
    return {"ran": True, "renamed": renamed}


def read_discord_bridge(agent_id, home=None):
    """Read the bridge state for one agent."""
    path = _discord_bridge_path(home, agent_id)
    if not path.exists():
        return None
    return _read_json(path)


def remove_discord_bridge(agent_id, home=None):
    """Remove the bridge state for one agent."""
    path = _discord_bridge_path(home, agent_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def ingest_discord(home=None, hostname=None, now=None):
    """Receive Discord messages and update bridge buffers."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    host = hostname or current_hostname()
    if not config.get("enabled") or config.get("owner_hostname") != host:
        return {"ran": False, "received": 0, "buffered": 0, "commands": 0}
    received = 0
    buffered = 0
    handled = 0
    for bridge in list_discord_bridges(home):
        if not bridge.get("active", True) or not bridge.get("channel_id"):
            continue
        changed = False
        for event in _channel_messages(config, bridge["channel_id"], bridge.get("last_message_id") or ""):
            bridge["last_message_id"] = event["id"]
            changed = True
            if event["author_id"] == config.get("bot_user_id"):
                continue
            user_id = config.get("user_id") or ""
            if user_id and event["author_id"] != user_id:
                continue
            if not event["text"]:
                continue
            received += 1
            try:
                if _handle_discord_command(home, host, bridge, event):
                    handled += 1
                    changed = True
                    continue
            except ValueError as exc:
                _send_channel_message(config, bridge["channel_id"], str(exc))
                handled += 1
                changed = True
                continue
            pending = list(bridge.get("pending_user_turn") or [])
            pending.append(
                {
                    "timestamp": event["timestamp"],
                    "author": event["author"],
                    "text": event["text"],
                }
            )
            bridge["pending_user_turn"] = pending
            buffered += 1
            changed = True
        if changed:
            _write_discord_bridge(bridge["agent_id"], bridge, home)
    return {"ran": True, "received": received, "buffered": buffered, "commands": handled}


def flush_discord_turns(home=None, hostname=None):
    """Flush buffered Discord user turns into agent messages."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    host = hostname or current_hostname()
    if not config.get("enabled") or config.get("owner_hostname") != host:
        return {"ran": False, "flushed": 0, "nudged": 0}
    from .agents import _read_json as read_json
    from .agents import _run_lock_held, nudge_agent, send_agent

    flushed = 0
    nudged = 0
    for bridge in list_discord_bridges(home):
        pending = list(bridge.get("pending_user_turn") or [])
        if not pending or not bridge.get("active", True):
            continue
        agent_id = bridge["agent_id"]
        meta = read_json(home / "agents" / agent_id / "meta.json")
        lock_path = home / "agents" / agent_id / "hosts" / meta["hostname"] / "run.lock"
        if _run_lock_held(lock_path):
            continue
        body = _format_pending_turn(pending)
        if not body:
            bridge["pending_user_turn"] = []
            _write_discord_bridge(agent_id, bridge, home)
            continue
        author = pending[0].get("author") or "discord"
        if len(pending) > 1:
            author = "discord"
        send_agent(agent_id, body, author, home=home, hostname=host)
        bridge["pending_user_turn"] = []
        _write_discord_bridge(agent_id, bridge, home)
        flushed += 1
        if meta["hostname"] == host:
            nudge_agent(agent_id, home=home, hostname=host, wait=False)
            nudged += 1
    return {"ran": True, "flushed": flushed, "nudged": nudged}


def deliver_discord_updates(home=None, hostname=None, agent_id=None):
    """Send unsent completed turn updates to bridged Discord channels."""
    home = _resolve_home(home)
    config = read_discord_config(home)
    host = hostname or current_hostname()
    if not config.get("enabled") or config.get("owner_hostname") != host:
        return {"ran": False, "sent": 0}
    sent = 0
    bridges = []
    if agent_id:
        bridge = read_discord_bridge(agent_id, home)
        if bridge:
            bridges.append(bridge)
    else:
        bridges = list_discord_bridges(home)
    for bridge in bridges:
        if not bridge.get("active", True) or not bridge.get("channel_id"):
            continue
        unsent = _unsent_runs(home, bridge)
        if not unsent:
            continue
        for run in unsent:
            text = _run_update_text(run)
            if not text:
                bridge["last_sent_run_id"] = run.get("id") or bridge.get("last_sent_run_id") or ""
                continue
            _send_channel_message(config, bridge["channel_id"], text)
            bridge["last_sent_run_id"] = run.get("id") or bridge.get("last_sent_run_id") or ""
            sent += 1
        _write_discord_bridge(bridge["agent_id"], bridge, home)
    return {"ran": True, "sent": sent}


def _resolve_home(home):
    if home is None:
        return codexapi_home()
    return Path(home).expanduser().resolve()


def _ensure_discord_dirs(home):
    discord_dir(home).mkdir(parents=True, exist_ok=True)
    _discord_bridges_dir(home).mkdir(parents=True, exist_ok=True)


def _discord_config_path(home=None):
    return discord_dir(home) / "config.json"


def _discord_bridges_dir(home=None):
    return discord_dir(home) / "bridges"


def _discord_bridge_path(home, agent_id):
    return _discord_bridges_dir(home) / f"{agent_id}.json"


def _write_discord_config(config, home=None):
    path = _discord_config_path(home)
    _write_json(path, config)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _write_discord_bridge(agent_id, payload, home=None):
    _write_json(_discord_bridge_path(home, agent_id), payload)


def _group_description(agent_meta):
    prompt = " ".join(str(agent_meta.get("prompt") or "").split())
    prompt = prompt[: _CHANNEL_TOPIC_LIMIT - 12].strip()
    if len(prompt) >= _CHANNEL_TOPIC_LIMIT - 12:
        prompt = prompt[:-3].rstrip() + "..."
    return f"{prompt} [{agent_meta['id'][:8]}]".strip()


def _desired_channel_name(agent_meta, home):
    state = _read_json(home / "agents" / agent_meta["id"] / "state.json")
    status = str(state.get("status") or "").strip()
    return _channel_name(agent_meta["name"], status)


def _channel_name(name, status):
    prefix = _STATUS_EMOJI.get(str(status or "").strip(), "")
    slug = _slugify(name)
    if not prefix:
        return slug[:_CHANNEL_NAME_LIMIT]
    title = f"{prefix}-{slug}"
    return title[:_CHANNEL_NAME_LIMIT]


def _slugify(name):
    text = str(name or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "agent"


def _permission_overwrites(config):
    guild_id = str(config.get("guild_id") or "").strip()
    user_id = str(config.get("user_id") or "").strip()
    bot_user_id = str(config.get("bot_user_id") or "").strip()
    return [
        {
            "id": guild_id,
            "type": _TYPE_ROLE,
            "allow": "0",
            "deny": str(_PERM_VIEW_CHANNEL),
        },
        {
            "id": user_id,
            "type": _TYPE_MEMBER,
            "allow": str(_USER_PERMISSIONS),
            "deny": "0",
        },
        {
            "id": bot_user_id,
            "type": _TYPE_MEMBER,
            "allow": str(_BOT_PERMISSIONS),
            "deny": "0",
        },
    ]


def _sync_channel_name(config, bridge, home):
    agent_dir = home / "agents" / bridge["agent_id"]
    if not agent_dir.exists():
        return False
    agent_meta = _read_json(agent_dir / "meta.json")
    desired = _desired_channel_name(agent_meta, home)
    if bridge.get("channel_name") == desired:
        return False
    _discord_request(
        config,
        "PATCH",
        f"/channels/{bridge['channel_id']}",
        body={"name": desired},
    )
    bridge["channel_name"] = desired
    _write_discord_bridge(bridge["agent_id"], bridge, home)
    return True


def _create_discord_bridge(agent_meta, config, home):
    payload = {
        "name": _desired_channel_name(agent_meta, home),
        "type": _TYPE_TEXT_CHANNEL,
        "topic": _group_description(agent_meta),
        "permission_overwrites": _permission_overwrites(config),
    }
    category_id = str(config.get("category_id") or "").strip()
    if category_id:
        payload["parent_id"] = category_id
    channel = _discord_request(config, "POST", f"/guilds/{config['guild_id']}/channels", body=payload)
    bridge = {
        "agent_id": agent_meta["id"],
        "channel_id": str(channel.get("id") or "").strip(),
        "channel_name": payload["name"],
        "last_message_id": "",
        "pending_user_turn": [],
        "last_sent_run_id": "",
        "active": True,
    }
    if not bridge["channel_id"]:
        raise ValueError(f"Discord channel id missing for {agent_meta['name']!r}")
    _write_discord_bridge(agent_meta["id"], bridge, home)
    return bridge


def _discord_request(config, method, path, body=None, query=None):
    token = str(config.get("bot_token") or "").strip()
    if not token:
        raise ValueError("Discord bot token is not configured")
    url = _API_BASE + path
    if query:
        encoded = urlencode(query)
        if encoded:
            url += "?" + encoded
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "Accept": "application/json",
        "User-Agent": "codexapi-discord/1.0",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, method=method, data=data, headers=headers)
    for _ in range(2):
        try:
            with urlopen(request) as response:
                raw = response.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
        except HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            payload = _safe_json(raw)
            if exc.code == 429:
                retry_after = payload.get("retry_after")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = 1.0
                time.sleep(max(delay, 0.1))
                continue
            message = str(payload.get("message") or raw or f"Discord API error {exc.code}").strip()
            raise ValueError(message) from None
        except URLError as exc:
            raise ValueError(str(exc.reason) or "Discord API request failed") from None
    raise ValueError("Discord API rate limit retry exhausted")


def _safe_json(text):
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _channel_messages(config, channel_id, after_id):
    channel_id = str(channel_id or "").strip()
    cursor = str(after_id or "").strip()
    events = []
    while True:
        query = {"limit": str(_CHANNEL_LIMIT)}
        if cursor:
            query["after"] = cursor
        batch = _discord_request(config, "GET", f"/channels/{channel_id}/messages", query=query)
        if not isinstance(batch, list) or not batch:
            break
        ordered = sorted(batch, key=lambda item: int(item.get("id") or "0"))
        for payload in ordered:
            event = _message_event(payload)
            if event is not None:
                events.append(event)
        cursor = str(ordered[-1].get("id") or cursor)
        if len(ordered) < _CHANNEL_LIMIT:
            break
    return events


def _message_event(payload):
    if not isinstance(payload, dict):
        return None
    author = payload.get("author") or {}
    author_id = str(author.get("id") or "").strip()
    message_id = str(payload.get("id") or "").strip()
    channel_id = str(payload.get("channel_id") or "").strip()
    text = str(payload.get("content") or "").strip()
    if not message_id or not channel_id:
        return None
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author_id": author_id,
        "author": _user_name(author),
        "text": text,
        "timestamp": str(payload.get("timestamp") or "").strip(),
    }


def _user_name(payload):
    return (
        str(payload.get("global_name") or "").strip()
        or str(payload.get("display_name") or "").strip()
        or str(payload.get("username") or "").strip()
        or str(payload.get("id") or "").strip()
    )


def _handle_discord_command(home, host, bridge, event):
    command, arg = _parse_discord_command(event["text"])
    if not command:
        return False
    from .agents import (
        control_agent,
        delete_agent,
        nudge_agent,
        read_agentbook,
        set_agent_heartbeat,
        show_agent,
        status_agent,
    )

    config = read_discord_config(home)
    agent_id = bridge["agent_id"]
    channel_id = bridge["channel_id"]
    if command == "help":
        _send_channel_message(config, channel_id, _HELP_TEXT)
        return True
    if command == "status":
        shown = show_agent(agent_id, home=home)
        status = status_agent(agent_id, home=home)
        text = (
            f"{shown['name']} [{shown['status']}]\n"
            f"Activity: {shown['activity'] or '-'}\n"
            f"Reply: {shown['reply'] or '-'}\n"
            f"Update: {shown.get('update') or '-'}\n"
            f"Turn: {status.get('turn_id') or '-'} [{status.get('turn_state') or '-'}]"
        )
        _send_channel_message(config, channel_id, text)
        return True
    if command == "book":
        book = read_agentbook(agent_id, home=home)
        text = (book.get("text") or "").strip() or "(empty)"
        _send_channel_message(config, channel_id, text[-_BOOK_LIMIT:])
        return True
    if command == "set-heartbeat":
        minutes = int(arg or "0")
        result = set_agent_heartbeat(agent_id, minutes, home=home)
        _send_channel_message(
            config,
            channel_id,
            f"Heartbeat set to {result['heartbeat_minutes']}m. Next wake: {result['next_wake_at'] or '-'}",
        )
        return True
    if command == "delete":
        if arg != "confirm":
            _send_channel_message(config, channel_id, "Refusing delete without: !delete confirm")
            return True
        delete_agent(agent_id, home=home)
        _send_channel_message(config, channel_id, "Agent deleted.")
        remove_discord_bridge(agent_id, home)
        return True
    control_agent(agent_id, command, event["author"], home=home, hostname=host)
    if command in ("wake", "resume", "pause", "cancel"):
        shown = show_agent(agent_id, home=home)
        if shown["hostname"] == host:
            nudge_agent(agent_id, home=home, hostname=host, wait=False)
    _send_channel_message(config, channel_id, f"{command} queued.")
    return True


def _parse_discord_command(text):
    stripped = str(text or "").strip()
    if not stripped.startswith("!"):
        return "", ""
    command, _, rest = stripped[1:].partition(" ")
    command = command.strip().lower()
    rest = rest.strip()
    if command in ("wake", "pause", "resume", "cancel", "status", "book", "help"):
        return command, rest
    if command == "set-heartbeat":
        if not rest or not rest.isdigit():
            raise ValueError("!set-heartbeat requires an integer minute value")
        return command, rest
    if command == "delete":
        return command, rest
    raise ValueError(f"unknown discord command: {command}")


def _format_pending_turn(items):
    if not items:
        return ""
    if len(items) == 1:
        return (items[0].get("text") or "").strip()
    lines = []
    for item in items:
        stamp = item.get("timestamp") or ""
        author = item.get("author") or "discord"
        text = item.get("text") or ""
        lines.append(f"[{stamp}] {author}: {text}")
    return "\n".join(lines).strip()


def _send_channel_message(config, channel_id, text):
    message = str(text or "").strip()
    if not message:
        return
    _discord_request(
        config,
        "POST",
        f"/channels/{channel_id}/messages",
        body={"content": message},
    )


def _unsent_runs(home, bridge):
    from .agents import _read_json as read_json

    agent_id = bridge["agent_id"]
    meta = read_json(home / "agents" / agent_id / "meta.json")
    runs_dir = home / "agents" / agent_id / "hosts" / meta["hostname"] / "runs"
    if not runs_dir.exists():
        return []
    last_sent = bridge.get("last_sent_run_id") or ""
    runs = []
    for path in sorted(runs_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix != ".json":
            continue
        run = read_json(path)
        run_id = run.get("id") or ""
        if last_sent and run_id <= last_sent:
            continue
        if not run.get("ended_at"):
            continue
        runs.append(run)
    return runs


def _run_update_text(run):
    update = str(run.get("update") or "").strip()
    if update:
        return update
    error = str(run.get("error") or "").strip()
    if error:
        return f"I hit an error: {error}"
    reply = str(run.get("reply") or "").strip()
    if reply:
        return reply
    return str(run.get("status") or "").strip()
