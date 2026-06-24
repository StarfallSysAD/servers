# Tailscale MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for managing
[Tailscale](https://tailscale.com) resources deployed via the
[Tailscale Kubernetes Operator](https://tailscale.com/kb/1236/kubernetes-operator).

This server exposes Tailscale Kubernetes CRDs (Connectors, ProxyClasses, etc.) and
operator-managed pods as MCP tools and resources so LLMs can inspect and manage
your Tailscale network directly from a chat interface.

## Tools

| Tool | Description |
|------|-------------|
| `list-devices` | List all Tailscale node pods in the cluster |
| `get-device` | Describe a specific Tailscale device pod |
| `list-exit-nodes` | List pods/connectors configured as exit nodes |
| `set-exit-node` | Enable or disable exit-node advertising on a Connector |
| `list-connectors` | List Tailscale `Connector` CRDs |
| `create-connector` | Create a new Tailscale `Connector` CRD |
| `delete-connector` | Delete a Tailscale `Connector` CRD |
| `get-acl-policy` | Read the ACL policy ConfigMap managed by the operator |
| `get-operator-status` | Check the Tailscale operator deployment health |
| `restart-device` | Restart a Tailscale node by deleting its pod |

## Resources

| URI | Description |
|-----|-------------|
| `tailscale://devices` | Live list of all Tailscale node pods |
| `tailscale://connectors` | All Tailscale Connector CRDs |
| `tailscale://exit-nodes` | Filtered view of exit-node-tagged devices |

## Configuration

### CLI flags

```
mcp-server-tailscale [OPTIONS]

Options:
  --namespace TEXT   Kubernetes namespace to watch (default: tailscale)
  --in-cluster       Use in-cluster service account credentials
  --kubeconfig TEXT  Path to kubeconfig file (default: ~/.kube/config)
  -v, --verbose      Increase verbosity (use -vv for debug)
  --help             Show this message and exit.
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `TAILSCALE_NAMESPACE` | Kubernetes namespace (default: `tailscale`) |
| `KUBECONFIG` | Path to kubeconfig file |

## Usage

### With Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tailscale": {
      "command": "uvx",
      "args": ["mcp-server-tailscale", "--namespace", "tailscale"]
    }
  }
}
```

### In-cluster (Kubernetes pod)

Deploy alongside the Tailscale operator and pass `--in-cluster`:

```json
{
  "mcpServers": {
    "tailscale": {
      "command": "mcp-server-tailscale",
      "args": ["--in-cluster", "--namespace", "tailscale"]
    }
  }
}
```

## Prerequisites

- Tailscale Kubernetes Operator installed in your cluster
- `kubectl` access or in-cluster service account with permissions to:
  - `get`, `list`, `watch` pods and deployments in the Tailscale namespace
  - `get`, `list`, `create`, `delete` Tailscale CRDs (`connectors.tailscale.com`)
  - `get`, `list` ConfigMaps in the Tailscale namespace
