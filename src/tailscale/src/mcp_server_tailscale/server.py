"""MCP server for managing Tailscale via the Kubernetes Operator."""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any, Sequence, cast

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    EmbeddedResource,
    ImageContent,
    Resource,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import AnyUrl, BaseModel, Field

logger = logging.getLogger(__name__)

# Tailscale Kubernetes Operator API group / version
TS_GROUP = "tailscale.com"
TS_VERSION = "v1alpha1"
CONNECTOR_PLURAL = "connectors"


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class ListDevices(BaseModel):
    namespace: str = Field("tailscale", description="Kubernetes namespace to search")


class GetDevice(BaseModel):
    name: str = Field(..., description="Pod name of the Tailscale device")
    namespace: str = Field("tailscale", description="Kubernetes namespace")


class ListExitNodes(BaseModel):
    namespace: str = Field("tailscale", description="Kubernetes namespace to search")


class SetExitNode(BaseModel):
    name: str = Field(..., description="Name of the Connector CRD")
    namespace: str = Field("tailscale", description="Kubernetes namespace")
    enabled: bool = Field(..., description="Whether to enable exit-node advertising")


class ListConnectors(BaseModel):
    namespace: str = Field("tailscale", description="Kubernetes namespace to search")


class CreateConnector(BaseModel):
    name: str = Field(..., description="Name for the new Connector CRD")
    namespace: str = Field("tailscale", description="Kubernetes namespace")
    subnet_router: list[str] = Field(
        default_factory=list,
        description="CIDR ranges to advertise as subnet routes (e.g. ['10.0.0.0/8'])",
    )
    exit_node: bool = Field(False, description="Whether to advertise as an exit node")
    hostname: str | None = Field(
        None,
        description="Tailscale hostname for this connector (defaults to the CRD name)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="ACL tags for this connector (e.g. ['tag:connector'])",
    )


class DeleteConnector(BaseModel):
    name: str = Field(..., description="Name of the Connector CRD to delete")
    namespace: str = Field("tailscale", description="Kubernetes namespace")


class GetAclPolicy(BaseModel):
    namespace: str = Field("tailscale", description="Kubernetes namespace")
    configmap_name: str = Field(
        "tailscale-acl",
        description="Name of the ConfigMap holding the ACL policy",
    )


class GetOperatorStatus(BaseModel):
    namespace: str = Field("tailscale", description="Kubernetes namespace")
    deployment_name: str = Field(
        "operator",
        description="Name of the Tailscale operator Deployment",
    )


class RestartDevice(BaseModel):
    name: str = Field(..., description="Pod name to delete (will be recreated by its controller)")
    namespace: str = Field("tailscale", description="Kubernetes namespace")


# ---------------------------------------------------------------------------
# Tool name enum
# ---------------------------------------------------------------------------


class TailscaleTools(str, Enum):
    LIST_DEVICES = "list-devices"
    GET_DEVICE = "get-device"
    LIST_EXIT_NODES = "list-exit-nodes"
    SET_EXIT_NODE = "set-exit-node"
    LIST_CONNECTORS = "list-connectors"
    CREATE_CONNECTOR = "create-connector"
    DELETE_CONNECTOR = "delete-connector"
    GET_ACL_POLICY = "get-acl-policy"
    GET_OPERATOR_STATUS = "get-operator-status"
    RESTART_DEVICE = "restart-device"


# ---------------------------------------------------------------------------
# Helper: load Kubernetes client config
# ---------------------------------------------------------------------------


def _load_k8s_config(in_cluster: bool, kubeconfig: str | None) -> None:
    """Load the Kubernetes client configuration."""
    if in_cluster:
        config.load_incluster_config()
    else:
        config.load_kube_config(config_file=kubeconfig)


# ---------------------------------------------------------------------------
# Business-logic helpers (pure K8s calls, easy to mock)
# ---------------------------------------------------------------------------


def _pod_to_dict(pod: client.V1Pod) -> dict[str, Any]:
    """Extract a concise summary from a V1Pod object."""
    meta = pod.metadata or client.V1ObjectMeta()
    status = pod.status or client.V1PodStatus()
    labels: dict[str, str] = meta.labels or {}
    return {
        "name": meta.name,
        "namespace": meta.namespace,
        "phase": status.phase,
        "pod_ip": status.pod_ip,
        "host_ip": status.host_ip,
        "hostname": labels.get("tailscale.com/hostname", meta.name),
        "labels": labels,
        "annotations": meta.annotations or {},
        "node_name": spec.node_name if (spec := pod.spec) else None,
    }


def list_devices(core_v1: client.CoreV1Api, namespace: str) -> list[dict[str, Any]]:
    """Return all pods with the tailscale.com label selector."""
    label_selector = "app.kubernetes.io/managed-by=tailscale-operator"
    resp = cast(
        client.V1PodList,
        core_v1.list_namespaced_pod(namespace, label_selector=label_selector),
    )
    return [_pod_to_dict(p) for p in (resp.items or [])]


def get_device(core_v1: client.CoreV1Api, name: str, namespace: str) -> dict[str, Any]:
    """Return detailed info about a single Tailscale pod."""
    pod = cast(
        client.V1Pod,
        core_v1.read_namespaced_pod(name=name, namespace=namespace),
    )
    return _pod_to_dict(pod)


def list_exit_nodes(
    core_v1: client.CoreV1Api,
    custom: client.CustomObjectsApi,
    namespace: str,
) -> list[dict[str, Any]]:
    """Return Connector CRDs that have exitNode=true, plus annotated pods."""
    results: list[dict[str, Any]] = []

    # Check Connectors with exitNode enabled
    try:
        connectors = cast(
            dict[str, Any],
            custom.list_namespaced_custom_object(
                group=TS_GROUP, version=TS_VERSION, namespace=namespace, plural=CONNECTOR_PLURAL
            ),
        )
        for item in connectors.get("items", []):
            spec = item.get("spec", {})
            if spec.get("exitNode", False):
                results.append(
                    {
                        "kind": "Connector",
                        "name": item["metadata"]["name"],
                        "namespace": item["metadata"]["namespace"],
                        "exit_node": True,
                        "subnet_router": spec.get("subnetRouter", {}).get("advertiseRoutes", []),
                    }
                )
    except ApiException as exc:
        if exc.status != 404:
            raise

    return results


def list_connectors(
    custom: client.CustomObjectsApi, namespace: str
) -> list[dict[str, Any]]:
    """Return all Connector CRDs in the namespace."""
    try:
        resp = cast(
            dict[str, Any],
            custom.list_namespaced_custom_object(
                group=TS_GROUP, version=TS_VERSION, namespace=namespace, plural=CONNECTOR_PLURAL
            ),
        )
    except ApiException as exc:
        if exc.status == 404:
            return []
        raise
    results = []
    for item in resp.get("items", []):
        spec = item.get("spec", {})
        results.append(
            {
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
                "exit_node": spec.get("exitNode", False),
                "subnet_router": spec.get("subnetRouter", {}).get("advertiseRoutes", []),
                "hostname": spec.get("hostname"),
                "tags": spec.get("tags", []),
                "conditions": item.get("status", {}).get("conditions", []),
            }
        )
    return results


def set_exit_node(
    custom: client.CustomObjectsApi,
    name: str,
    namespace: str,
    enabled: bool,
) -> dict[str, Any]:
    """Patch a Connector CRD to enable/disable exit-node advertising."""
    patch_body = {"spec": {"exitNode": enabled}}
    result = cast(
        dict[str, Any],
        custom.patch_namespaced_custom_object(
            group=TS_GROUP,
            version=TS_VERSION,
            namespace=namespace,
            plural=CONNECTOR_PLURAL,
            name=name,
            body=patch_body,
        ),
    )
    spec = result.get("spec", {})
    return {
        "name": result["metadata"]["name"],
        "exit_node": spec.get("exitNode", False),
        "message": f"Exit-node advertising {'enabled' if enabled else 'disabled'} on Connector '{name}'",
    }


def create_connector(
    custom: client.CustomObjectsApi,
    name: str,
    namespace: str,
    subnet_router: list[str],
    exit_node: bool,
    hostname: str | None,
    tags: list[str],
) -> dict[str, Any]:
    """Create a new Tailscale Connector CRD."""
    spec: dict[str, Any] = {
        "hostname": hostname or name,
        "tags": tags,
        "exitNode": exit_node,
    }
    if subnet_router:
        spec["subnetRouter"] = {"advertiseRoutes": subnet_router}

    body: dict[str, Any] = {
        "apiVersion": f"{TS_GROUP}/{TS_VERSION}",
        "kind": "Connector",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }
    result = cast(
        dict[str, Any],
        custom.create_namespaced_custom_object(
            group=TS_GROUP,
            version=TS_VERSION,
            namespace=namespace,
            plural=CONNECTOR_PLURAL,
            body=body,
        ),
    )
    return {
        "name": result["metadata"]["name"],
        "namespace": result["metadata"]["namespace"],
        "message": f"Connector '{name}' created successfully",
    }


def delete_connector(
    custom: client.CustomObjectsApi,
    name: str,
    namespace: str,
) -> dict[str, Any]:
    """Delete a Tailscale Connector CRD."""
    custom.delete_namespaced_custom_object(
        group=TS_GROUP,
        version=TS_VERSION,
        namespace=namespace,
        plural=CONNECTOR_PLURAL,
        name=name,
    )
    return {"name": name, "message": f"Connector '{name}' deleted successfully"}


def get_acl_policy(
    core_v1: client.CoreV1Api,
    namespace: str,
    configmap_name: str,
) -> dict[str, Any]:
    """Read the ACL policy from a Kubernetes ConfigMap."""
    try:
        cm = cast(
            client.V1ConfigMap,
            core_v1.read_namespaced_config_map(name=configmap_name, namespace=namespace),
        )
    except ApiException as exc:
        if exc.status == 404:
            return {
                "found": False,
                "message": f"ConfigMap '{configmap_name}' not found in namespace '{namespace}'",
            }
        raise
    data = cm.data or {}
    return {
        "found": True,
        "name": cm.metadata.name if cm.metadata else configmap_name,
        "namespace": namespace,
        "data": data,
    }


def get_operator_status(
    apps_v1: client.AppsV1Api,
    namespace: str,
    deployment_name: str,
) -> dict[str, Any]:
    """Return health/rollout status of the Tailscale operator Deployment."""
    try:
        dep = cast(
            client.V1Deployment,
            apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace),
        )
    except ApiException as exc:
        if exc.status == 404:
            return {
                "found": False,
                "message": f"Deployment '{deployment_name}' not found in namespace '{namespace}'",
            }
        raise

    status = dep.status or client.V1DeploymentStatus()
    spec = dep.spec or client.V1DeploymentSpec(selector=client.V1LabelSelector())
    return {
        "found": True,
        "name": deployment_name,
        "namespace": namespace,
        "desired": spec.replicas,
        "ready": status.ready_replicas,
        "available": status.available_replicas,
        "updated": status.updated_replicas,
        "conditions": [
            {"type": c.type, "status": c.status, "message": c.message}
            for c in (status.conditions or [])
        ],
    }


def restart_device(
    core_v1: client.CoreV1Api,
    name: str,
    namespace: str,
) -> dict[str, Any]:
    """Delete a Tailscale pod so its controller restarts it."""
    core_v1.delete_namespaced_pod(name=name, namespace=namespace)
    return {
        "name": name,
        "namespace": namespace,
        "message": f"Pod '{name}' deleted; it will be restarted by its controller",
    }


# ---------------------------------------------------------------------------
# MCP serve()
# ---------------------------------------------------------------------------


async def serve(
    namespace: str = "tailscale",
    in_cluster: bool = False,
    kubeconfig: str | None = None,
) -> None:
    """Start the MCP server."""
    _load_k8s_config(in_cluster, kubeconfig)

    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    custom = client.CustomObjectsApi()

    server = Server("mcp-tailscale")

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(
                uri="tailscale://devices",  # type: ignore[arg-type]
                name="Tailscale Devices",
                description="Live list of all Tailscale node pods managed by the operator",
                mimeType="application/json",
            ),
            Resource(
                uri="tailscale://connectors",  # type: ignore[arg-type]
                name="Tailscale Connectors",
                description="All Tailscale Connector CRDs in the namespace",
                mimeType="application/json",
            ),
            Resource(
                uri="tailscale://exit-nodes",  # type: ignore[arg-type]
                name="Tailscale Exit Nodes",
                description="Connectors and devices configured as exit nodes",
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        uri_str = str(uri)
        match uri_str:
            case "tailscale://devices":
                devices = list_devices(core_v1, namespace)
                return json.dumps(devices, indent=2)
            case "tailscale://connectors":
                connectors = list_connectors(custom, namespace)
                return json.dumps(connectors, indent=2)
            case "tailscale://exit-nodes":
                exit_nodes = list_exit_nodes(core_v1, custom, namespace)
                return json.dumps(exit_nodes, indent=2)
            case _:
                raise ValueError(f"Unknown resource URI: {uri_str}")

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=TailscaleTools.LIST_DEVICES,
                description=(
                    "List all Tailscale node pods managed by the Kubernetes operator"
                ),
                inputSchema=ListDevices.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.GET_DEVICE,
                description="Describe a specific Tailscale device pod (status, IP, labels)",
                inputSchema=GetDevice.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.LIST_EXIT_NODES,
                description="List Tailscale Connectors and pods configured as exit nodes",
                inputSchema=ListExitNodes.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.SET_EXIT_NODE,
                description="Enable or disable exit-node advertising on a Tailscale Connector CRD",
                inputSchema=SetExitNode.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.LIST_CONNECTORS,
                description="List all Tailscale Connector CRDs in the namespace",
                inputSchema=ListConnectors.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.CREATE_CONNECTOR,
                description=(
                    "Create a new Tailscale Connector CRD to advertise subnet routes "
                    "or act as an exit node"
                ),
                inputSchema=CreateConnector.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.DELETE_CONNECTOR,
                description="Delete a Tailscale Connector CRD",
                inputSchema=DeleteConnector.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.GET_ACL_POLICY,
                description=(
                    "Read the Tailscale ACL policy from a Kubernetes ConfigMap "
                    "managed by the operator"
                ),
                inputSchema=GetAclPolicy.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.GET_OPERATOR_STATUS,
                description="Check the health and rollout status of the Tailscale operator Deployment",
                inputSchema=GetOperatorStatus.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            ),
            Tool(
                name=TailscaleTools.RESTART_DEVICE,
                description=(
                    "Restart a Tailscale device by deleting its pod "
                    "(the pod will be recreated by its Deployment/StatefulSet controller)"
                ),
                inputSchema=RestartDevice.model_json_schema(),
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
            ),
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
        match name:
            case TailscaleTools.LIST_DEVICES:
                params = ListDevices(**arguments)
                result = list_devices(core_v1, params.namespace)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.GET_DEVICE:
                params = GetDevice(**arguments)
                result = get_device(core_v1, params.name, params.namespace)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.LIST_EXIT_NODES:
                params = ListExitNodes(**arguments)
                result = list_exit_nodes(core_v1, custom, params.namespace)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.SET_EXIT_NODE:
                params = SetExitNode(**arguments)
                result = set_exit_node(custom, params.name, params.namespace, params.enabled)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.LIST_CONNECTORS:
                params = ListConnectors(**arguments)
                result = list_connectors(custom, params.namespace)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.CREATE_CONNECTOR:
                params = CreateConnector(**arguments)
                result = create_connector(
                    custom,
                    params.name,
                    params.namespace,
                    params.subnet_router,
                    params.exit_node,
                    params.hostname,
                    params.tags,
                )
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.DELETE_CONNECTOR:
                params = DeleteConnector(**arguments)
                result = delete_connector(custom, params.name, params.namespace)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.GET_ACL_POLICY:
                params = GetAclPolicy(**arguments)
                result = get_acl_policy(core_v1, params.namespace, params.configmap_name)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.GET_OPERATOR_STATUS:
                params = GetOperatorStatus(**arguments)
                result = get_operator_status(apps_v1, params.namespace, params.deployment_name)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case TailscaleTools.RESTART_DEVICE:
                params = RestartDevice(**arguments)
                result = restart_device(core_v1, params.name, params.namespace)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]

            case _:
                raise ValueError(f"Unknown tool: {name}")

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)
