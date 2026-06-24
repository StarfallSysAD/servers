"""Tests for mcp_server_tailscale.server using mocked Kubernetes clients."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from mcp_server_tailscale.server import (
    TS_GROUP,
    TS_VERSION,
    CONNECTOR_PLURAL,
    create_connector,
    delete_connector,
    get_acl_policy,
    get_device,
    get_operator_status,
    list_connectors,
    list_devices,
    list_exit_nodes,
    restart_device,
    set_exit_node,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pod(
    name: str = "ts-pod-1",
    namespace: str = "tailscale",
    phase: str = "Running",
    pod_ip: str = "100.64.0.1",
    labels: dict | None = None,
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.labels = labels or {"app.kubernetes.io/managed-by": "tailscale-operator"}
    pod.metadata.annotations = {}
    pod.status.phase = phase
    pod.status.pod_ip = pod_ip
    pod.status.host_ip = "192.168.1.1"
    pod.spec.node_name = "node-1"
    return pod


def _make_connector_item(
    name: str = "my-connector",
    namespace: str = "tailscale",
    exit_node: bool = False,
    routes: list[str] | None = None,
    hostname: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "exitNode": exit_node,
            "subnetRouter": {"advertiseRoutes": routes or []},
            "hostname": hostname,
            "tags": tags or [],
        },
        "status": {"conditions": []},
    }


# ---------------------------------------------------------------------------
# list_devices
# ---------------------------------------------------------------------------


def test_list_devices_returns_pods():
    core_v1 = MagicMock()
    core_v1.list_namespaced_pod.return_value.items = [_make_pod("ts-pod-1"), _make_pod("ts-pod-2")]

    result = list_devices(core_v1, "tailscale")

    assert len(result) == 2
    assert result[0]["name"] == "ts-pod-1"
    assert result[1]["name"] == "ts-pod-2"
    core_v1.list_namespaced_pod.assert_called_once_with(
        "tailscale", label_selector="app.kubernetes.io/managed-by=tailscale-operator"
    )


def test_list_devices_empty_namespace():
    core_v1 = MagicMock()
    core_v1.list_namespaced_pod.return_value.items = []

    result = list_devices(core_v1, "tailscale")

    assert result == []


# ---------------------------------------------------------------------------
# get_device
# ---------------------------------------------------------------------------


def test_get_device_returns_pod_info():
    core_v1 = MagicMock()
    core_v1.read_namespaced_pod.return_value = _make_pod("ts-pod-1", pod_ip="100.64.0.5")

    result = get_device(core_v1, "ts-pod-1", "tailscale")

    assert result["name"] == "ts-pod-1"
    assert result["pod_ip"] == "100.64.0.5"
    assert result["phase"] == "Running"
    core_v1.read_namespaced_pod.assert_called_once_with(name="ts-pod-1", namespace="tailscale")


# ---------------------------------------------------------------------------
# list_exit_nodes
# ---------------------------------------------------------------------------


def test_list_exit_nodes_connector_with_exit_node():
    core_v1 = MagicMock()
    custom = MagicMock()
    custom.list_namespaced_custom_object.return_value = {
        "items": [
            _make_connector_item("exit-cn", exit_node=True, routes=["0.0.0.0/0"]),
            _make_connector_item("subnet-cn", exit_node=False),
        ]
    }

    result = list_exit_nodes(core_v1, custom, "tailscale")

    assert len(result) == 1
    assert result[0]["name"] == "exit-cn"
    assert result[0]["exit_node"] is True


def test_list_exit_nodes_no_connectors_crd():
    core_v1 = MagicMock()
    custom = MagicMock()
    exc = ApiException(status=404, reason="Not Found")
    custom.list_namespaced_custom_object.side_effect = exc

    result = list_exit_nodes(core_v1, custom, "tailscale")

    assert result == []


def test_list_exit_nodes_propagates_non_404():
    core_v1 = MagicMock()
    custom = MagicMock()
    exc = ApiException(status=500, reason="Internal Server Error")
    custom.list_namespaced_custom_object.side_effect = exc

    with pytest.raises(ApiException):
        list_exit_nodes(core_v1, custom, "tailscale")


# ---------------------------------------------------------------------------
# list_connectors
# ---------------------------------------------------------------------------


def test_list_connectors_returns_all():
    custom = MagicMock()
    custom.list_namespaced_custom_object.return_value = {
        "items": [
            _make_connector_item("cn1"),
            _make_connector_item("cn2", exit_node=True),
        ]
    }

    result = list_connectors(custom, "tailscale")

    assert len(result) == 2
    assert result[0]["name"] == "cn1"
    assert result[1]["exit_node"] is True


def test_list_connectors_empty_on_404():
    custom = MagicMock()
    exc = ApiException(status=404, reason="Not Found")
    custom.list_namespaced_custom_object.side_effect = exc

    result = list_connectors(custom, "tailscale")

    assert result == []


# ---------------------------------------------------------------------------
# set_exit_node
# ---------------------------------------------------------------------------


def test_set_exit_node_enable():
    custom = MagicMock()
    custom.patch_namespaced_custom_object.return_value = {
        "metadata": {"name": "cn1"},
        "spec": {"exitNode": True},
    }

    result = set_exit_node(custom, "cn1", "tailscale", enabled=True)

    assert result["exit_node"] is True
    assert "enabled" in result["message"]
    custom.patch_namespaced_custom_object.assert_called_once_with(
        group=TS_GROUP,
        version=TS_VERSION,
        namespace="tailscale",
        plural=CONNECTOR_PLURAL,
        name="cn1",
        body={"spec": {"exitNode": True}},
    )


def test_set_exit_node_disable():
    custom = MagicMock()
    custom.patch_namespaced_custom_object.return_value = {
        "metadata": {"name": "cn1"},
        "spec": {"exitNode": False},
    }

    result = set_exit_node(custom, "cn1", "tailscale", enabled=False)

    assert result["exit_node"] is False
    assert "disabled" in result["message"]


# ---------------------------------------------------------------------------
# create_connector
# ---------------------------------------------------------------------------


def test_create_connector_minimal():
    custom = MagicMock()
    custom.create_namespaced_custom_object.return_value = {
        "metadata": {"name": "new-cn", "namespace": "tailscale"},
    }

    result = create_connector(
        custom,
        name="new-cn",
        namespace="tailscale",
        subnet_router=[],
        exit_node=False,
        hostname=None,
        tags=[],
    )

    assert result["name"] == "new-cn"
    assert "created successfully" in result["message"]


def test_create_connector_with_routes_and_exit_node():
    custom = MagicMock()
    custom.create_namespaced_custom_object.return_value = {
        "metadata": {"name": "exit-cn", "namespace": "tailscale"},
    }

    result = create_connector(
        custom,
        name="exit-cn",
        namespace="tailscale",
        subnet_router=["10.0.0.0/8"],
        exit_node=True,
        hostname="my-exit",
        tags=["tag:connector"],
    )

    assert result["name"] == "exit-cn"
    called_body = custom.create_namespaced_custom_object.call_args.kwargs["body"]
    assert called_body["spec"]["exitNode"] is True
    assert called_body["spec"]["subnetRouter"]["advertiseRoutes"] == ["10.0.0.0/8"]
    assert called_body["spec"]["hostname"] == "my-exit"


# ---------------------------------------------------------------------------
# delete_connector
# ---------------------------------------------------------------------------


def test_delete_connector():
    custom = MagicMock()

    result = delete_connector(custom, "old-cn", "tailscale")

    assert result["name"] == "old-cn"
    assert "deleted successfully" in result["message"]
    custom.delete_namespaced_custom_object.assert_called_once()


# ---------------------------------------------------------------------------
# get_acl_policy
# ---------------------------------------------------------------------------


def test_get_acl_policy_found():
    core_v1 = MagicMock()
    cm = MagicMock()
    cm.metadata.name = "tailscale-acl"
    cm.data = {"acl.hujson": '{"acls": []}'}
    core_v1.read_namespaced_config_map.return_value = cm

    result = get_acl_policy(core_v1, "tailscale", "tailscale-acl")

    assert result["found"] is True
    assert "acl.hujson" in result["data"]


def test_get_acl_policy_not_found():
    core_v1 = MagicMock()
    exc = ApiException(status=404, reason="Not Found")
    core_v1.read_namespaced_config_map.side_effect = exc

    result = get_acl_policy(core_v1, "tailscale", "tailscale-acl")

    assert result["found"] is False
    assert "not found" in result["message"]


def test_get_acl_policy_propagates_non_404():
    core_v1 = MagicMock()
    exc = ApiException(status=403, reason="Forbidden")
    core_v1.read_namespaced_config_map.side_effect = exc

    with pytest.raises(ApiException):
        get_acl_policy(core_v1, "tailscale", "tailscale-acl")


# ---------------------------------------------------------------------------
# get_operator_status
# ---------------------------------------------------------------------------


def test_get_operator_status_found():
    apps_v1 = MagicMock()
    dep = MagicMock()
    dep.spec.replicas = 1
    dep.status.ready_replicas = 1
    dep.status.available_replicas = 1
    dep.status.updated_replicas = 1
    dep.status.conditions = []
    apps_v1.read_namespaced_deployment.return_value = dep

    result = get_operator_status(apps_v1, "tailscale", "operator")

    assert result["found"] is True
    assert result["desired"] == 1
    assert result["ready"] == 1


def test_get_operator_status_not_found():
    apps_v1 = MagicMock()
    exc = ApiException(status=404, reason="Not Found")
    apps_v1.read_namespaced_deployment.side_effect = exc

    result = get_operator_status(apps_v1, "tailscale", "operator")

    assert result["found"] is False


# ---------------------------------------------------------------------------
# restart_device
# ---------------------------------------------------------------------------


def test_restart_device():
    core_v1 = MagicMock()

    result = restart_device(core_v1, "ts-pod-1", "tailscale")

    assert result["name"] == "ts-pod-1"
    assert "deleted" in result["message"]
    core_v1.delete_namespaced_pod.assert_called_once_with(name="ts-pod-1", namespace="tailscale")
