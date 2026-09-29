# gVisor Sandbox for the n8n AI Agent (Dokploy)

[Türkçe](BENIOKU.md)

Gives the AI Agent in n8n a safe terminal. Every chat gets its own isolated container;
containers run under **gVisor (runsc)**, cannot reach n8n's network or secrets, and are deleted when idle.

```
n8n AI Agent ── MCP Client Tool ──▶ sandbox-mcp (this repo) ──▶ sbx-<session> containers (gVisor)
                (HTTP Streamable,          token protected,          internet: open
                 Bearer token)             port not exposed          host / internal net / other containers: blocked
```

Agent tools: `run_command`, `run_python`, `write_file`, `read_file`, `reset_sandbox`.

## Files

| File | Purpose |
|---|---|
| `host/setup-host.sh` | Run **once** on the VPS over SSH: installs gVisor, registers it with Docker, sets up the firewall |
| `docker-compose.yml` | The service deployed in Dokploy |
| `server.py` | MCP server (creates/removes a sandbox per session) |
| `sandbox-image/Dockerfile` | The agent's environment (Python 3.12 + preinstalled packages). If you change it, the server rebuilds the image by itself |

---

## Step 1 — Install gVisor on the VPS (SSH, one time)

Copy `host/setup-host.sh` to the server (with scp, or open `nano setup-host.sh` and paste the content) and run it:

```bash
sudo bash setup-host.sh
```

At the end of the script you should see:

- `✓ Docker sees the runsc runtime`
- `✓ gVisor works`
- `✓ internet access works`
- `✓ the host (SSH ...) is NOT reachable from sandboxes`

Notes:
- Running containers (n8n, Dokploy) are not restarted; Docker is only `reload`ed.
- VPSes have no KVM, so gVisor runs in `systrap` mode; this is normal.
- The firewall rules apply to Docker's default `docker0` network and come back automatically after a reboot
  (`n8n-sandbox-firewall.service`). If you have no other containers on that network, nothing else is affected.

## Step 2 — Put the repo on GitHub

Push this folder to a **private** GitHub repo (Dokploy needs the source to build).

## Step 3 — Deploy in Dokploy

1. Generate a token (on your computer or on the server): `openssl rand -hex 32`
2. Dokploy → your project → **Create Service → Compose**
3. **Compose Type: Docker Compose** (not Stack — Stack mode does not support `build`)
4. Provider: GitHub → pick the repo and branch, **Compose Path:** `./docker-compose.yml`
5. **Environment** tab:
   ```
   MCP_TOKEN=<token from step 1>
   ```
   (Other settings are optional, see the table below.)
6. **Do not add a domain.** This service must only be reachable from the internal network.
7. **Deploy**. The first deploy builds the sandbox image (a few minutes). Expect this in the Logs:
   ```
   Sandbox image n8n-sandbox:latest ready
   MCP endpoint listening on http://0.0.0.0:8000/mcp
   ```

## Step 4 — Can n8n reach the service?

Over SSH:

```bash
# Both should be in the list
docker network inspect dokploy-network --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' | grep -iE "n8n|sandbox"

# Health check from inside the n8n container -> should print "ok"
N8N=$(docker ps --format '{{.Names}}' | grep -i n8n | grep -viE "worker|runner|postgres|redis" | head -1)
docker exec "$N8N" node -e "fetch('http://sandbox-mcp:8000/health').then(r=>r.text()).then(console.log)"
```

If n8n is not on `dokploy-network` (e.g. you did not add a domain to n8n, or Isolated Deployments is on),
add this to the n8n service in n8n's compose file and redeploy:

```yaml
services:
  n8n:
    networks:
      - default
      - dokploy-network
networks:
  dokploy-network:
    external: true
```

## Step 5 — Connect it in n8n

1. **Credentials → New → Bearer Auth**: Token = your `MCP_TOKEN` value.
2. In the AI Agent node, add **Tool → MCP Client Tool**:
   - **Endpoint:** `http://sandbox-mcp:8000/mcp`
   - **Server Transport:** HTTP Streamable
   - **Authentication:** Bearer Auth → the credential from above
   - **Tools to Include:** All
   - If Options has a **Timeout**, set it to `600000` (10 min). Otherwise long commands time out on the n8n side;
     that is why the default command timeout is kept at 55 s.
3. Add this to the AI Agent's **System Message** (if you use the Chat Trigger):

   ```
   You have access to a Linux sandbox (run_command, run_python, write_file, read_file, reset_sandbox).
   ALWAYS use this as session_id in every sandbox tool: {{ $('When chat message received').item.json.sessionId }}
   Do complex work step by step: first write the code to a file, then run it, read the error and fix it.
   Read result files with read_file and summarize them for the user.
   ```

   If you use another trigger such as Telegram, pass the chat ID as session_id, e.g.
   `{{ $('Telegram Trigger').item.json.message.chat.id }}`.

## Step 6 — Test

Send these to the chat in order:

1. `Show the first line of the dmesg command and the python version in the sandbox.` → the output should contain **Starting gVisor**.
2. `Use pandas to create a table with the squares of the numbers from 1 to 10, save it as result.csv and show its content.`
3. `Try to connect to port 22 of 172.17.0.1.` → it should fail (this means the firewall works).

In Dokploy, every command appears in the sandbox-mcp logs as `session=... run_command: ...`.

---

## Settings (Dokploy Environment)

| Variable | Default | Description |
|---|---|---|
| `MCP_TOKEN` | — (required) | At least 24 characters. `openssl rand -hex 32` |
| `SANDBOX_NETWORK` | `bridge` | `none` = no internet at all in the sandbox (pip install will not work, the safest option) |
| `SANDBOX_DNS` | `1.1.1.1,9.9.9.9` | DNS servers used by the sandboxes |
| `MAX_SANDBOXES` | `5` | Maximum number of concurrent sessions |
| `SANDBOX_MEMORY` | `1g` | RAM per sandbox |
| `SANDBOX_CPUS` | `1.0` | CPU per sandbox |
| `IDLE_TIMEOUT_MINUTES` | `30` | An idle sandbox is deleted after this long |
| `MAX_LIFETIME_MINUTES` | `360` | Maximum lifetime of a sandbox |
| `DEFAULT_TIMEOUT_SECONDS` | `55` | Default time per command (raise it if you increased the n8n timeout) |
| `MAX_TIMEOUT_SECONDS` | `900` | Longest timeout the agent can request |

If you need extra packages/tools, add them to `sandbox-image/Dockerfile`, push and redeploy; the image is rebuilt automatically.

## Security notes

- `sandbox-mcp` has access to the Docker socket, so it is **equivalent to root on the host**. Never give it a domain or a port,
  and never share the token.
- No API keys / env variables are passed to the sandboxes. Data the agent puts into the sandbox can leave to the internet
  (since internet is open); if you work with sensitive data, use `SANDBOX_NETWORK=none`.
- Sandboxes: gVisor + non-root user + all Linux capabilities dropped + `no-new-privileges`
  + RAM/CPU/process limits + host, internal networks, the cloud metadata address and SMTP 25 blocked.
- Keep gVisor up to date: `sudo apt-get update && sudo apt-get install --only-upgrade runsc`

## Troubleshooting

| Symptom | Fix |
|---|---|
| Logs: `Docker runtime 'runsc' is not installed` | Run Step 1, then redeploy |
| Logs: `MCP_TOKEN must be set...` | Add a token of at least 24 characters to Environment |
| Deploy: `network dokploy-network not found` | Check with `docker network ls`; make sure the Compose Type is "Docker Compose" |
| n8n: cannot connect / `ENOTFOUND sandbox-mcp` | Step 4 — they are not on the same network |
| n8n: 401 | Wrong Bearer token |
| n8n: timeout | Increase the MCP Client Tool Timeout or lower `DEFAULT_TIMEOUT_SECONDS` |
| `pip install` / DNS does not work in the sandbox | Test with `docker run --rm --runtime=runsc --dns 1.1.1.1 busybox nslookup pypi.org`; if your provider blocks external DNS, set another DNS with `SANDBOX_DNS` |
| `Sandbox limit reached` | Increase `MAX_SANDBOXES` or lower `IDLE_TIMEOUT_MINUTES` |
| Dokploy cleanup deleted the sandbox image | Not a problem: the image is rebuilt automatically on the first command; that first call may time out, try again after a few minutes |
