"""Constants for Cluster Guard."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "cluster_guard"
VERSION = "0.1.0"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN_ID = "token_id"  # user@realm!tokenname
CONF_TOKEN_SECRET = "token_secret"
CONF_VERIFY_SSL = "verify_ssl"
CONF_HA_VMID = "ha_vmid"  # the VM Home Assistant itself runs in (optional)
CONF_STALE_MINUTES = "stale_minutes"
CONF_POLL_SECONDS = "poll_seconds"

DEFAULT_PORT = 8006
DEFAULT_VERIFY_SSL = False  # Proxmox ships a self-signed certificate
DEFAULT_STALE_MINUTES = 10  # a one-minute replication schedule; 10 missed runs is a problem
DEFAULT_POLL_SECONDS = 30
UNREACHABLE_POLLS = 4  # consecutive failed polls before we say the cluster API is unreachable

POLL_INTERVAL = timedelta(seconds=DEFAULT_POLL_SECONDS)

ISSUE_NODE_DOWN = "node_down"
ISSUE_DEGRADED = "degraded"
ISSUE_QUORUM = "quorum_lost"
ISSUE_REPLICATION = "replication_stale"
ISSUE_HA_RESOURCE = "ha_resource_error"
ISSUE_UNREACHABLE = "cluster_unreachable"
ISSUE_NO_HA = "ha_vm_not_protected"

EVENT_NODE_DOWN = f"{DOMAIN}_node_down"
EVENT_NODE_BACK = f"{DOMAIN}_node_back"
EVENT_FAILOVER = f"{DOMAIN}_failover"

NOTIFICATION_PREFIX = f"{DOMAIN}_"
