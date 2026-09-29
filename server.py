"""
n8n Sandbox MCP server
======================

Gives an n8n AI Agent a terminal: every chat session gets its own throw-away
container running under gVisor (runsc). The agent reaches this server through
n8n's "MCP Client Tool" node (HTTP Streamable transport, Bearer auth).

Tools exposed to the agent:
  run_command    - run a bash command in the session sandbox
  run_python     - run a Python snippet in the session sandbox
  write_file     - create/overwrite a file in the sandbox
  read_file      - read a file from the sandbox (text or base64)
  reset_sandbox  - destroy the session sandbox and start fresh next time
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import anyio
import docker
import uvicorn
from docker.errors import APIError, ImageNotFound, NotFound
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


# --------------------------------------------------------------------------- #
# Configuration (all via environment variables)
# --------------------------------------------------------------------------- #
def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


MCP_TOKEN = os.getenv("MCP_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8000"))

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "n8n-sandbox:latest")
SANDBOX_IMAGE_CONTEXT = os.getenv("SANDBOX_IMAGE_CONTEXT", "/app/sandbox-image")
BUILD_IMAGE = _env_bool("BUILD_SANDBOX_IMAGE", True)

SANDBOX_RUNTIME = os.getenv("SANDBOX_RUNTIME", "runsc")
ALLOW_UNSAFE_RUNTIME = _env_bool("ALLOW_UNSAFE_RUNTIME", False)  # testing only
SANDBOX_NETWORK = os.getenv("SANDBOX_NETWORK", "bridge")  # "bridge" or "none"
SANDBOX_DNS = [d.strip() for d in os.getenv("SANDBOX_DNS", "1.1.1.1,9.9.9.9").split(",") if d.strip()]
SANDBOX_MEMORY = os.getenv("SANDBOX_MEMORY", "1g")
SANDBOX_CPUS = float(os.getenv("SANDBOX_CPUS", "1.0"))
SANDBOX_PIDS = int(os.getenv("SANDBOX_PIDS", "256"))
SANDBOX_USER = os.getenv("SANDBOX_USER", "1000:1000")
WORKDIR = "/workspace"

MAX_SANDBOXES = int(os.getenv("MAX_SANDBOXES", "5"))
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT_MINUTES", "30")) * 60
MAX_LIFETIME = int(os.getenv("MAX_LIFETIME_MINUTES", "360")) * 60

DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT_SECONDS", "55"))
MAX_TIMEOUT = int(os.getenv("MAX_TIMEOUT_SECONDS", "900"))
MAX_OUTPUT_CHARS = int(os.getenv("MAX_OUTPUT_CHARS", "12000"))

LABEL_MANAGED = "n8n-sandbox.managed"
LABEL_SESSION = "n8n-sandbox.session"
LABEL_IMAGE_HASH = "n8n-sandbox.context-hash"
NAME_PREFIX = "sbx-"
_WRITE_CHUNK = 96_000  # base64 chars per exec call; multiple of 4, below the 128 KiB argv limit


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Docker helpers
# --------------------------------------------------------------------------- #
client = docker.from_env(timeout=MAX_TIMEOUT + 60)

_state_lock = threading.Lock()
_session_locks: dict[str, threading.Lock] = {}
_last_used: dict[str, float] = {}
_image_lock = threading.Lock()


def _context_hash() -> str:
    """Hash of the sandbox image build context, so edits trigger a rebuild."""
    h = hashlib.sha256()
    root = Path(SANDBOX_IMAGE_CONTEXT)
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def ensure_image() -> None:
    """Build the sandbox image if it is missing or its build context changed."""
    with _image_lock:
        want = _context_hash() if (BUILD_IMAGE and Path(SANDBOX_IMAGE_CONTEXT).is_dir()) else None
        try:
            img = client.images.get(SANDBOX_IMAGE)
            if want is None or img.labels.get(LABEL_IMAGE_HASH) == want:
                return
            log(f"Sandbox image context changed, rebuilding {SANDBOX_IMAGE}")
        except ImageNotFound:
            if want is None:
                raise RuntimeError(
                    f"Sandbox image '{SANDBOX_IMAGE}' not found and BUILD_SANDBOX_IMAGE is off."
                )
            log(f"Sandbox image {SANDBOX_IMAGE} not found, building it (first run takes a few minutes)")

        for chunk in client.api.build(
            path=SANDBOX_IMAGE_CONTEXT,
            tag=SANDBOX_IMAGE,
            rm=True,
            pull=True,
            decode=True,
            labels={LABEL_IMAGE_HASH: want},
        ):
            if "stream" in chunk and chunk["stream"].strip():
                print(chunk["stream"].rstrip(), flush=True)
            if "error" in chunk:
                raise RuntimeError(f"Sandbox image build failed: {chunk['error']}")
        log(f"Sandbox image {SANDBOX_IMAGE} ready")


def _safe_session(session_id: str) -> str:
    session_id = (session_id or "").strip()
    if not session_id:
        raise ValueError("session_id is required.")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}", session_id):
        return session_id
    # Anything else (emails, spaces, very long ids...) is hashed into a stable name.
    return "h" + hashlib.sha256(session_id.encode()).hexdigest()[:24]


def _session_lock(sid: str) -> threading.Lock:
    with _state_lock:
        return _session_locks.setdefault(sid, threading.Lock())


def _managed_containers():
    return client.containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=true"})


def _create_container(sid: str):
    kwargs = dict(
        image=SANDBOX_IMAGE,
        name=NAME_PREFIX + sid,
        command=["sleep", "infinity"],
        detach=True,
        runtime=SANDBOX_RUNTIME,
        user=SANDBOX_USER,
        working_dir=WORKDIR,
        hostname="sandbox",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        mem_limit=SANDBOX_MEMORY,
        memswap_limit=SANDBOX_MEMORY,
        nano_cpus=int(SANDBOX_CPUS * 1e9),
        pids_limit=SANDBOX_PIDS,
        labels={LABEL_MANAGED: "true", LABEL_SESSION: sid},
        environment={},  # never pass secrets into the sandbox
    )
    if SANDBOX_NETWORK == "none":
        kwargs["network_mode"] = "none"
    else:
        # The default "bridge" network is used on purpose: gVisor cannot reach
        # Docker's embedded DNS on user-defined networks, while the default
        # bridge honours the explicit DNS servers below.
        kwargs["network"] = SANDBOX_NETWORK
        kwargs["dns"] = SANDBOX_DNS
    try:
        return client.containers.run(**kwargs)
    except ImageNotFound:
        ensure_image()
        return client.containers.run(**kwargs)


def get_sandbox(session_id: str):
    sid = _safe_session(session_id)
    with _session_lock(sid):
        try:
            c = client.containers.get(NAME_PREFIX + sid)
            if c.status != "running":
                c.remove(force=True)
                raise NotFound("not running")
        except NotFound:
            running = [x for x in _managed_containers() if x.status == "running"]
            if len(running) >= MAX_SANDBOXES:
                raise RuntimeError(
                    f"Sandbox limit reached ({MAX_SANDBOXES} active). Try again later "
                    "or call reset_sandbox for a session that is no longer needed."
                )
            c = _create_container(sid)
            log(f"session={sid} sandbox created ({c.short_id})")
        _last_used[sid] = time.time()
        return c


def _touch(session_id: str) -> None:
    _last_used[_safe_session(session_id)] = time.time()


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    skipped = len(text) - head - tail
    return f"{text[:head]}\n\n... [{skipped} characters truncated] ...\n\n{text[-tail:]}"


def _clamp_timeout(t: int | None) -> int:
    if not t or t <= 0:
        return DEFAULT_TIMEOUT
    return min(int(t), MAX_TIMEOUT)


def _exec(container, argv: list[str], timeout: int, workdir: str = WORKDIR):
    wrapped = ["timeout", "--kill-after=5", f"{timeout}s", *argv]
    res = container.exec_run(wrapped, workdir=workdir, user=SANDBOX_USER, demux=True)
    out, err = res.output if res.output else (None, None)
    return (
        res.exit_code,
        (out or b"").decode("utf-8", errors="replace"),
        (err or b"").decode("utf-8", errors="replace"),
    )


def _format_result(exit_code: int, out: str, err: str, timeout: int) -> str:
    parts = [f"exit_code: {exit_code}"]
    if exit_code in (124, 137):
        parts.append(f"NOTE: the process was stopped by the {timeout}s time limit.")
    parts.append("--- stdout ---\n" + (_truncate(out) if out else "(empty)"))
    if err:
        parts.append("--- stderr ---\n" + _truncate(err))
    return "\n".join(parts)


def _resolve(path: str) -> str:
    path = (path or "").strip()
    if not path:
        raise ValueError("path is required.")
    return path if path.startswith("/") else f"{WORKDIR}/{path}"


def _write_bytes(container, path: str, data: bytes) -> None:
    b64 = base64.b64encode(data).decode()
    chunks = [b64[i : i + _WRITE_CHUNK] for i in range(0, len(b64), _WRITE_CHUNK)] or [""]
    for i, chunk in enumerate(chunks):
        redirect = ">" if i == 0 else ">>"
        script = f'mkdir -p "$(dirname "$1")" && printf %s "$2" | base64 -d {redirect} "$1"'
        code, _, err = _exec(container, ["sh", "-c", script, "sh", path, chunk], timeout=60)
        if code != 0:
            raise RuntimeError(f"Could not write {path}: {err.strip() or 'exit code ' + str(code)}")


# --------------------------------------------------------------------------- #
# Tool implementations (sync, run in a worker thread)
# --------------------------------------------------------------------------- #
def _run_command(session_id: str, command: str, timeout_seconds: int | None) -> str:
    if not command or not command.strip():
        raise ValueError("command is empty.")
    t = _clamp_timeout(timeout_seconds)
    c = get_sandbox(session_id)
    log(f"session={_safe_session(session_id)} run_command: {command[:200]!r}")
    code, out, err = _exec(c, ["bash", "-c", command], timeout=t)
    _touch(session_id)
    return _format_result(code, out, err, t)


def _run_python(session_id: str, code_str: str, timeout_seconds: int | None) -> str:
    if not code_str or not code_str.strip():
        raise ValueError("code is empty.")
    t = _clamp_timeout(timeout_seconds)
    c = get_sandbox(session_id)
    log(f"session={_safe_session(session_id)} run_python: {len(code_str)} chars")
    script = f"/tmp/agent_{int(time.time() * 1000)}.py"
    _write_bytes(c, script, code_str.encode())
    code, out, err = _exec(c, ["python3", script], timeout=t)
    _touch(session_id)
    return _format_result(code, out, err, t)


def _write_file(session_id: str, path: str, content: str, encoding: str) -> str:
    target = _resolve(path)
    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"content is not valid base64: {e}") from e
    else:
        data = (content or "").encode("utf-8")
    c = get_sandbox(session_id)
    log(f"session={_safe_session(session_id)} write_file: {target} ({len(data)} bytes)")
    _write_bytes(c, target, data)
    return f"Wrote {len(data)} bytes to {target}"


def _read_file(session_id: str, path: str, encoding: str, max_chars: int | None) -> str:
    target = _resolve(path)
    limit = max(1, min(int(max_chars or 20000), 200_000))
    c = get_sandbox(session_id)
    log(f"session={_safe_session(session_id)} read_file: {target}")
    code, _, err = _exec(c, ["test", "-f", target], timeout=10)
    if code != 0:
        raise FileNotFoundError(f"{target} does not exist or is not a regular file.")
    _, size_s, _ = _exec(c, ["stat", "-c", "%s", target], timeout=10)
    size = int(size_s.strip() or 0)
    if encoding == "base64":
        max_bytes = limit * 3 // 4
        if size > max_bytes:
            raise ValueError(
                f"{target} is {size} bytes; base64 output is limited to {max_bytes} bytes. "
                "Raise max_chars or compress/split the file first."
            )
        code, out, err = _exec(c, ["base64", "-w0", target], timeout=60)
        if code != 0:
            raise RuntimeError(err.strip() or f"base64 failed with exit code {code}")
        return out.strip()
    code, out, err = _exec(c, ["head", "-c", str(limit * 4), target], timeout=30)
    if code != 0:
        raise RuntimeError(err.strip() or f"read failed with exit code {code}")
    text = out[:limit]
    if size > len(text.encode("utf-8", errors="replace")):
        text += f"\n\n... [file is {size} bytes, output limited to {limit} characters]"
    return text


def _reset_sandbox(session_id: str) -> str:
    sid = _safe_session(session_id)
    with _session_lock(sid):
        try:
            client.containers.get(NAME_PREFIX + sid).remove(force=True)
            _last_used.pop(sid, None)
            log(f"session={sid} sandbox reset by agent")
            return "Sandbox destroyed. The next command will start a fresh, empty sandbox."
        except NotFound:
            return "There was no sandbox for this session."


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
_net_note = (
    "The sandbox has NO network access."
    if SANDBOX_NETWORK == "none"
    else "The sandbox can reach the public internet (pip install works) but not the "
    "server's internal network."
)

INSTRUCTIONS = f"""\
Isolated Linux sandbox (Debian, Python 3.12, bash) running under gVisor.
- Every tool needs a session_id. Always pass the same session_id for one conversation;
  files in {WORKDIR} persist between calls of the same session.
- A sandbox is deleted after {IDLE_TIMEOUT // 60} idle minutes. Nothing in it is visible to
  n8n unless you return it (e.g. read_file).
- You run as an unprivileged user without sudo. Install Python packages with
  `pip install <pkg>` (goes to the user site). Pre-installed: requests, httpx, pandas, numpy,
  matplotlib, beautifulsoup4, lxml, openpyxl, pillow, pyyaml.
- Commands time out after {DEFAULT_TIMEOUT}s by default (max {MAX_TIMEOUT}s); long output is truncated.
- {_net_note}
"""

mcp = FastMCP(
    name="n8n-sandbox",
    instructions=INSTRUCTIONS,
    log_level="WARNING",
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
    # Only reachable on the internal Docker network and protected by a token,
    # so Host-header checks (which would reject "sandbox-mcp:8000") are off.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def run_command(session_id: str, command: str, timeout_seconds: int = DEFAULT_TIMEOUT) -> str:
    """Run a bash command inside the isolated sandbox for this session.

    The working directory is /workspace. Use this for shell work: listing files,
    installing packages (pip install ...), running scripts, git, curl, etc.
    Returns the exit code, stdout and stderr.

    Args:
        session_id: Conversation/session identifier. Use the same value for the whole conversation.
        command: The bash command to run, e.g. "ls -la" or "pip install pandas && python script.py".
        timeout_seconds: Maximum run time in seconds.
    """
    return await anyio.to_thread.run_sync(partial(_run_command, session_id, command, timeout_seconds))


@mcp.tool()
async def run_python(session_id: str, code: str, timeout_seconds: int = DEFAULT_TIMEOUT) -> str:
    """Run a Python 3 program inside the isolated sandbox for this session.

    The code is saved to a temporary file and executed with python3 from /workspace.
    Print whatever you need to see; only stdout/stderr are returned. Variables do NOT
    persist between calls, so save intermediate results to files in /workspace.

    Args:
        session_id: Conversation/session identifier. Use the same value for the whole conversation.
        code: Full Python source code to execute.
        timeout_seconds: Maximum run time in seconds.
    """
    return await anyio.to_thread.run_sync(partial(_run_python, session_id, code, timeout_seconds))


@mcp.tool()
async def write_file(session_id: str, path: str, content: str, encoding: str = "text") -> str:
    """Create or overwrite a file in the sandbox. Parent folders are created automatically.

    Args:
        session_id: Conversation/session identifier.
        path: File path. Relative paths are placed under /workspace.
        content: File content (plain text, or base64 when encoding="base64").
        encoding: "text" (default) or "base64" for binary files.
    """
    if encoding not in ("text", "base64"):
        raise ValueError('encoding must be "text" or "base64".')
    return await anyio.to_thread.run_sync(partial(_write_file, session_id, path, content, encoding))


@mcp.tool()
async def read_file(session_id: str, path: str, encoding: str = "text", max_chars: int = 20000) -> str:
    """Read a file from the sandbox.

    Use encoding="base64" to get binary files (images, xlsx, pdf) back as base64 text.

    Args:
        session_id: Conversation/session identifier.
        path: File path. Relative paths are resolved under /workspace.
        encoding: "text" (default) or "base64".
        max_chars: Maximum number of characters to return.
    """
    if encoding not in ("text", "base64"):
        raise ValueError('encoding must be "text" or "base64".')
    return await anyio.to_thread.run_sync(partial(_read_file, session_id, path, encoding, max_chars))


@mcp.tool()
async def reset_sandbox(session_id: str) -> str:
    """Destroy this session's sandbox (all files are lost). Use when the environment is broken
    or the task is finished and the files are no longer needed.

    Args:
        session_id: Conversation/session identifier.
    """
    return await anyio.to_thread.run_sync(partial(_reset_sandbox, session_id))


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request):
    return PlainTextResponse("ok")


# --------------------------------------------------------------------------- #
# Bearer-token middleware (pure ASGI so streaming responses are untouched)
# --------------------------------------------------------------------------- #
class BearerAuth:
    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            ok = auth[:7].lower() == b"bearer " and hmac.compare_digest(auth[7:].strip(), self.token)
            if not ok:
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
# Idle reaper
# --------------------------------------------------------------------------- #
def _parse_created(c) -> float:
    raw = c.attrs.get("Created", "")
    try:
        raw = re.sub(r"\.(\d{6})\d*", r".\1", raw).replace("Z", "+00:00")
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return time.time()


def reaper_loop() -> None:
    while True:
        time.sleep(60)
        try:
            now = time.time()
            for c in _managed_containers():
                sid = c.labels.get(LABEL_SESSION, "")
                last = _last_used.setdefault(sid, now)  # unknown (e.g. after restart) -> grace period
                idle = now - last
                age = now - _parse_created(c)
                if c.status != "running" or idle > IDLE_TIMEOUT or age > MAX_LIFETIME:
                    with _session_lock(sid):
                        c.remove(force=True)
                        _last_used.pop(sid, None)
                    reason = "stopped" if c.status != "running" else ("idle" if idle > IDLE_TIMEOUT else "max lifetime")
                    log(f"session={sid} sandbox removed ({reason})")
        except (APIError, NotFound) as e:
            log(f"reaper warning: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"reaper error: {e!r}")


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def preflight() -> None:
    if len(MCP_TOKEN) < 24:
        sys.exit("MCP_TOKEN must be set and at least 24 characters long (use: openssl rand -hex 32).")
    try:
        info = client.info()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Cannot talk to Docker ({e}). Is /var/run/docker.sock mounted?")
    runtimes = set((info.get("Runtimes") or {}).keys())
    if SANDBOX_RUNTIME not in runtimes:
        sys.exit(
            f"Docker runtime '{SANDBOX_RUNTIME}' is not installed (available: {sorted(runtimes)}). "
            "Install gVisor on the host first (host/setup-host.sh)."
        )
    if SANDBOX_RUNTIME != "runsc" and not ALLOW_UNSAFE_RUNTIME:
        sys.exit("Refusing to run sandboxes without gVisor. Set SANDBOX_RUNTIME=runsc.")
    if SANDBOX_NETWORK not in ("bridge", "none"):
        log(f"WARNING: SANDBOX_NETWORK={SANDBOX_NETWORK}. gVisor containers cannot use Docker's "
            "embedded DNS on user-defined networks; name resolution will likely fail.")
    ensure_image()
    now = time.time()
    for c in _managed_containers():
        _last_used.setdefault(c.labels.get(LABEL_SESSION, ""), now)
    log(
        f"runtime={SANDBOX_RUNTIME} network={SANDBOX_NETWORK} max_sandboxes={MAX_SANDBOXES} "
        f"memory={SANDBOX_MEMORY} cpus={SANDBOX_CPUS} idle_timeout={IDLE_TIMEOUT // 60}m"
    )


def main() -> None:
    preflight()
    threading.Thread(target=reaper_loop, daemon=True, name="reaper").start()
    app = BearerAuth(mcp.streamable_http_app(), MCP_TOKEN)
    log(f"MCP endpoint listening on http://0.0.0.0:{PORT}/mcp")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
