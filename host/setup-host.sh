#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# n8n sandbox - one-time host setup (run on the VPS as root)
#
#   1. Installs gVisor (runsc) from the official apt repository
#   2. Registers "runsc" as a Docker runtime (no container restarts needed)
#   3. Installs firewall rules so sandboxes on docker0 can reach the internet
#      but NOT the host, other containers or private networks
#   4. Makes the firewall rules survive reboots / Docker restarts (systemd)
#
# Safe to run more than once.  Usage:  sudo bash setup-host.sh
# ---------------------------------------------------------------------------
set -euo pipefail

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root:  sudo bash $0"
command -v docker >/dev/null || die "Docker is not installed."
command -v apt-get >/dev/null || die "This script supports Debian/Ubuntu (apt) only."

# ---------------------------------------------------------------- 1. gVisor
say "Installing gVisor (runsc)"
if command -v runsc >/dev/null; then
  ok "runsc already installed: $(runsc --version 2>/dev/null | head -1)"
else
  apt-get update -qq
  apt-get install -y -qq apt-transport-https ca-certificates curl gnupg >/dev/null
  curl -fsSL https://gvisor.dev/archive.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    > /etc/apt/sources.list.d/gvisor.list
  apt-get update -qq
  apt-get install -y -qq runsc >/dev/null
  ok "installed: $(runsc --version | head -1)"
fi

if [[ -e /dev/kvm ]]; then
  ok "/dev/kvm exists (KVM available) - the default 'systrap' platform is still used, which is fine."
else
  ok "No /dev/kvm (normal on a VPS) - gVisor runs in 'systrap' mode, no KVM needed."
fi

# ------------------------------------------------------- 2. Docker runtime
say "Registering runsc as a Docker runtime"
[[ -f /etc/docker/daemon.json ]] && cp -n /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$(date +%s)" || true
runsc install
# 'reload' makes dockerd re-read daemon.json without restarting running containers.
systemctl reload docker
sleep 2
if docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
  ok "Docker sees the runsc runtime"
else
  warn "Docker did not pick up runsc after reload."
  warn "Run 'systemctl restart docker' in a quiet moment (restarts all containers briefly), then re-run this script."
  exit 1
fi

say "Smoke test: starting a container under gVisor"
if docker run --rm --runtime=runsc busybox dmesg 2>/dev/null | grep -qi gvisor; then
  ok "gVisor works"
else
  die "gVisor test failed. Check: docker run --rm --runtime=runsc busybox dmesg"
fi

# ------------------------------------------------------------ 3. Firewall
say "Installing sandbox firewall rules"
cat > /usr/local/sbin/n8n-sandbox-firewall.sh <<'FW'
#!/usr/bin/env bash
# Isolates containers on Docker's default bridge (docker0), where the n8n sandboxes run.
# Allowed: public internet (for pip install, APIs).  Blocked: the host itself,
# other containers, private/link-local networks (cloud metadata) and SMTP port 25.
set -euo pipefail
BR=docker0

if ! iptables -n -L DOCKER-USER >/dev/null 2>&1; then
  echo "DOCKER-USER chain not found (is Docker using the nftables firewall backend?)." >&2
  exit 1
fi

iptables -N SANDBOX-EGRESS 2>/dev/null || iptables -F SANDBOX-EGRESS
iptables -A SANDBOX-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SANDBOX-EGRESS -o "$BR" -j DROP                      # container <-> container
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
  iptables -A SANDBOX-EGRESS -d "$net" -j DROP                   # private nets + metadata
done
iptables -A SANDBOX-EGRESS -p tcp --dport 25 -j DROP             # no spam from your VPS IP
iptables -A SANDBOX-EGRESS -j RETURN
iptables -C DOCKER-USER -i "$BR" -j SANDBOX-EGRESS 2>/dev/null \
  || iptables -I DOCKER-USER 1 -i "$BR" -j SANDBOX-EGRESS

iptables -N SANDBOX-INPUT 2>/dev/null || iptables -F SANDBOX-INPUT
iptables -A SANDBOX-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A SANDBOX-INPUT -j DROP                                # nothing on the host (SSH, Dokploy, DBs...)
iptables -C INPUT -i "$BR" -j SANDBOX-INPUT 2>/dev/null \
  || iptables -I INPUT 1 -i "$BR" -j SANDBOX-INPUT

echo "n8n sandbox firewall rules applied on $BR"
FW
chmod 750 /usr/local/sbin/n8n-sandbox-firewall.sh

cat > /etc/systemd/system/n8n-sandbox-firewall.service <<'UNIT'
[Unit]
Description=Firewall rules isolating n8n agent sandboxes (docker0)
After=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/n8n-sandbox-firewall.sh

[Install]
WantedBy=docker.service
UNIT

systemctl daemon-reload
systemctl enable --now n8n-sandbox-firewall.service >/dev/null
systemctl restart n8n-sandbox-firewall.service
ok "Firewall rules active and enabled at boot"

say "Firewall test (a throw-away gVisor container on docker0)"
if docker run --rm --runtime=runsc --dns 1.1.1.1 busybox wget -q -T 5 -O /dev/null https://example.com 2>/dev/null; then
  ok "internet access works"
else
  warn "internet test failed (maybe the provider blocks it); set SANDBOX_NETWORK=none if you don't need internet"
fi
GW=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
SSH_PORT=$(ss -tlnpH 2>/dev/null | awk '/"sshd"/{n=split($4,a,":"); print a[n]; exit}')
SSH_PORT=${SSH_PORT:-22}
if docker run --rm --runtime=runsc busybox sh -c "nc -w 3 $GW $SSH_PORT </dev/null" >/dev/null 2>&1; then
  warn "the host's SSH ($GW:$SSH_PORT) is still reachable from docker0 - check your firewall setup"
else
  ok "the host (SSH $GW:$SSH_PORT) is NOT reachable from sandboxes"
fi

say "Done. Next: deploy the sandbox-mcp compose service in Dokploy (see README)."
