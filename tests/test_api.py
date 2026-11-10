"""The real ProxmoxClient against a recorded request log: the setup-time provisioning sequence."""

from __future__ import annotations

import pytest

from custom_components.cluster_guard.api import CG_PRIVS, ProxmoxAuthError, ProxmoxClient


class Recorder:
    """Stands in for ProxmoxClient._request: records calls, answers from a script."""

    def __init__(self, answers: dict[tuple[str, str], object]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs.get("data") or kwargs.get("params") or {}))
        return self.answers.get((method, path), {})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[ProxmoxClient, Recorder]:
    c = ProxmoxClient(session=None, host="pve1", port=8006)  # type: ignore[arg-type]
    rec = Recorder(
        {
            ("POST", "/access/ticket"): {"ticket": "T", "CSRFPreventionToken": "C"},
            ("GET", "/access/roles"): [{"roleid": "PVEAuditor"}],
            ("GET", "/access/users"): [{"userid": "root@pam"}],
            ("GET", "/access/users/homeassistant@pve/token"): [],
            ("POST", "/access/users/homeassistant@pve/token/cluster_guard"): {
                "full-tokenid": "homeassistant@pve!cluster_guard",
                "value": "s3cret",
            },
            ("GET", "/cluster/replication"): [{"id": "100-0"}],
        }
    )
    monkeypatch.setattr(c, "_request", rec)
    return c, rec


async def test_login_then_provision_creates_role_user_acl_and_token(client) -> None:
    c, rec = client
    await c.login("root@pam", "pw")
    assert c._headers == {"Cookie": "PVEAuthCookie=T", "CSRFPreventionToken": "C"}
    token_id, secret = await c.provision_token()
    assert (token_id, secret) == ("homeassistant@pve!cluster_guard", "s3cret")
    steps = [(m, p) for m, p, _ in rec.calls[1:]]
    assert steps == [
        ("GET", "/access/roles"),
        ("POST", "/access/roles"),
        ("GET", "/access/users"),
        ("POST", "/access/users"),
        ("PUT", "/access/acl"),
        ("GET", "/access/users/homeassistant@pve/token"),
        ("POST", "/access/users/homeassistant@pve/token/cluster_guard"),
    ]
    role = next(d for m, p, d in rec.calls if (m, p) == ("POST", "/access/roles"))
    assert role["roleid"] == "ClusterGuard" and set(role["privs"].split(",")) == set(CG_PRIVS)
    acl = next(d for m, p, d in rec.calls if (m, p) == ("PUT", "/access/acl"))
    assert acl == {"path": "/", "roles": "ClusterGuard", "users": "homeassistant@pve"}
    token = next(d for m, p, d in rec.calls if p.endswith("/token/cluster_guard"))
    assert token == {"privsep": 0}
    c.use_token(token_id, secret)
    assert c._headers == {"Authorization": "PVEAPIToken=homeassistant@pve!cluster_guard=s3cret"}


async def test_provision_refreshes_an_existing_role_and_token(client) -> None:
    c, rec = client
    rec.answers[("GET", "/access/roles")] = [{"roleid": "ClusterGuard"}]
    rec.answers[("GET", "/access/users")] = [{"userid": "homeassistant@pve"}]
    rec.answers[("GET", "/access/users/homeassistant@pve/token")] = [{"tokenid": "cluster_guard"}]
    await c.provision_token()
    steps = [(m, p) for m, p, _ in rec.calls]
    assert ("PUT", "/access/roles/ClusterGuard") in steps  # privileges brought up to date
    assert ("POST", "/access/roles") not in steps and ("POST", "/access/users") not in steps
    assert ("DELETE", "/access/users/homeassistant@pve/token/cluster_guard") in steps  # old token replaced


async def test_login_refused(client) -> None:
    c, rec = client
    rec.answers[("POST", "/access/ticket")] = {}
    with pytest.raises(ProxmoxAuthError):
        await c.login("root@pam", "wrong")


async def test_add_replication_picks_a_free_job_id(client) -> None:
    c, rec = client
    assert await c.add_replication(100, "pve3") == "100-1"
    assert rec.calls[-1] == (
        "POST",
        "/cluster/replication",
        {"id": "100-1", "type": "local", "target": "pve3", "schedule": "*/1"},
    )
