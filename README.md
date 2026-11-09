# Cluster Guard

Home Assistant integration (HACS) that watches the Proxmox VE cluster Home Assistant runs on and turns
degraded states into Repairs, the way Radio Guard does for radio coordinators.

A cluster that is *down* takes Home Assistant with it, and nothing inside HA can report that. What
Cluster Guard catches is everything before that point, where a person has days rather than minutes:

| Repair | Means | What you do |
| --- | --- | --- |
| Cluster node *X* is down | One node stopped responding; HA keeps running on another | Check power and cable, then the node's screen; rejoin or replace it |
| Only *n* of *N* nodes are up | Two or more nodes down; one more and HA stops on purpose | Fix nodes before anything else |
| Copies of your VMs are not being made | Replication is behind or failing; a failover would start from old data | Open the VM > Replication in Proxmox, read the log |
| Failover for *VM* is in state error | Proxmox's HA manager gave up on the resource | Datacenter > HA, fix the cause, set state back to started |
| The Home Assistant VM is not fully protected | No HA resource, or no copy on some node | Add the replication jobs and the HA resource |
| Cluster Guard cannot reach Proxmox | The node you pointed it at is unreachable | Point it at another node |

Entities: quorum lost, node *X* online (one per node), copy problem, nodes online, Home Assistant runs
on, oldest copy age, failover state. Events `cluster_guard_node_down`, `cluster_guard_node_back`,
`cluster_guard_failover` for your own notifications. One action, `cluster_guard.migrate`, live-moves a
VM to another node (use before taking a node down for maintenance).

## Setup

In Proxmox: Datacenter > Permissions > Users > Add `homeassistant@pve`; Permissions > Add > User
Permission, path `/`, role `PVEAuditor` (read-only); API Tokens > Add for that user, *Privilege
Separation* off. Copy the secret once.

In Home Assistant: Settings > Devices & services > Add integration > Cluster Guard: any node's
address, the token ID (`homeassistant@pve!name`) and secret, then pick the VM Home Assistant runs in.

## Development

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check custom_components tests && pytest -q
```
