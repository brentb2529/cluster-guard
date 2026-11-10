# Cluster Guard

Home Assistant integration (HACS) that watches the Proxmox VE cluster Home Assistant runs on and turns
degraded states into Repairs, the way Radio Guard does for radio coordinators. Set up and fixed entirely
from the Home Assistant UI: no command line, nothing to prepare on Proxmox.

A cluster that is *down* takes Home Assistant with it, and nothing inside HA can report that. What
Cluster Guard catches is everything before that point, where a person has days rather than minutes:

| Repair | Means | What you do |
| --- | --- | --- |
| Cluster node *X* is down | One node stopped responding; HA keeps running on another | Check power and cable, then the node's screen; rejoin or replace it |
| Only *n* of *N* cluster nodes are up | Two or more nodes down; one more and HA stops on purpose | Fix the dead nodes before anything else |
| Copies of your VMs are not being made | Replication is behind or failing; a failover would start from old data | Open VM > Replication in Proxmox and read the log |
| Failover for *VM* is in state error | Proxmox's HA manager gave up on the resource | Datacenter > HA, fix the cause, set the state back to started |
| The Home Assistant VM is not fully protected | No HA resource, or no copy on some node | **Press Fix**: Cluster Guard adds the resource and the replication jobs |
| Cluster Guard cannot reach Proxmox | No node answers the API | Check the network between HA and the nodes |

It talks to whichever node answers: when the node it was pointed at dies, it switches to another in the
same poll, so the one event it exists for never blinds it. During a failover only the node issue speaks;
the HA manager's fence/recovery states and the dead node's unreadable copies are not reported as faults.

Entities: quorum lost, node *X* online (one per node), copy problem, nodes online, Home Assistant runs
on, oldest copy age, failover state. Events `cluster_guard_node_down`, `cluster_guard_node_back`,
`cluster_guard_failover` for your own notifications; persistent notifications for node down and
failover. One action, `cluster_guard.migrate`, live-moves a VM to another node (before taking a node
down for maintenance).

## Setup

HACS > Integrations > Explore & add > Cluster Guard (or add this repository as a custom repository),
then Settings > Devices & services > Add integration > Cluster Guard:

1. Any node's address.
2. **Proxmox root password, used once (recommended).** Cluster Guard creates its own user
   `homeassistant@pve`, a role `ClusterGuard` with only the rights it needs (read the cluster, protect
   and move VMs), and an API token. The password is not stored. Or paste an existing API token.
3. Pick the VM Home Assistant runs in (the likely one is pre-selected).

If the HA VM is not yet protected, a Repair appears with a **Fix** button that adds the HA resource and
one-minute replication jobs to every other node.

Works alone. With [Radio Guard](https://github.com/brentb2529/radio-guard) installed, Radio Guard's
printable recovery sheet also lists the cluster.

## Development

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check custom_components tests && pytest -q
```
