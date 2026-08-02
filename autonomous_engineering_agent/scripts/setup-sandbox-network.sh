#!/usr/bin/env bash
# One-time (idempotent) host setup for the sandbox's dependency-install network.
#
# The agent worker launches sandboxed containers as siblings of the compose stack (via
# /var/run/docker.sock), not as compose services, so this network is not declared in
# docker-compose.yml. DockerSandbox itself will lazily `docker network create` this network
# on first use, but that only gives Docker's default inter-network isolation (which already
# blocks the sandbox from reaching postgres/redpanda/the dashboard on the compose network).
# It does NOT block the cloud metadata endpoint, since that's a normal routable IP, not a
# Docker network -- that requires the host iptables rule this script adds.
#
# Run this once per host after provisioning, and again any time the host's Docker/iptables
# state is reset. Safe to re-run.
set -euo pipefail

NETWORK_NAME="${SANDBOX_EGRESS_NETWORK:-patchpilot-sandbox-egress}"
SUBNET="${SANDBOX_EGRESS_SUBNET:-172.31.99.0/24}"
METADATA_IP="169.254.169.254"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required on PATH" >&2
  exit 1
fi

if docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
  echo "Docker network '$NETWORK_NAME' already exists"
else
  docker network create --driver bridge --subnet "$SUBNET" "$NETWORK_NAME"
  echo "Created Docker network '$NETWORK_NAME' ($SUBNET)"
fi

if ! command -v iptables >/dev/null 2>&1; then
  echo "iptables not found; cannot install the cloud metadata block. Install iptables and re-run." >&2
  exit 1
fi

# DOCKER-USER is evaluated before Docker's own dynamically-managed rules and, unlike raw
# FORWARD rules, survives `docker` daemon restarts and network churn.
if ! iptables -t filter -L DOCKER-USER >/dev/null 2>&1; then
  echo "DOCKER-USER chain not found; is the Docker daemon's iptables integration enabled?" >&2
  exit 1
fi

if iptables -C DOCKER-USER -s "$SUBNET" -d "$METADATA_IP" -j DROP 2>/dev/null; then
  echo "Metadata block for $SUBNET -> $METADATA_IP already present"
else
  iptables -I DOCKER-USER -s "$SUBNET" -d "$METADATA_IP" -j DROP
  echo "Installed metadata block for $SUBNET -> $METADATA_IP"
fi

cat <<EOF

Done. Verify with:
  docker network inspect $NETWORK_NAME
  iptables -L DOCKER-USER -n | grep $METADATA_IP

NOTE: this iptables rule is not persisted across a host reboot unless you use your
distro's persistence mechanism (e.g. iptables-persistent, netfilter-persistent, or a
systemd unit that re-runs this script on boot). Re-run this script after any reboot
if you have not set up persistence.
EOF
