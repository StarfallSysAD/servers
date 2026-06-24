"""MCP server for managing Tailscale via the Kubernetes Operator."""

import asyncio
import logging
import sys

import click

from .server import serve


@click.command()
@click.option(
    "--namespace",
    "-n",
    default="tailscale",
    show_default=True,
    help="Kubernetes namespace containing Tailscale resources",
)
@click.option(
    "--in-cluster",
    is_flag=True,
    default=False,
    help="Use in-cluster service account credentials (for running inside a pod)",
)
@click.option(
    "--kubeconfig",
    default=None,
    help="Path to kubeconfig file (defaults to ~/.kube/config or KUBECONFIG env var)",
)
@click.option("-v", "--verbose", count=True)
def main(
    namespace: str,
    in_cluster: bool,
    kubeconfig: str | None,
    verbose: int,
) -> None:
    """MCP Tailscale Server - Manage Tailscale via the Kubernetes Operator."""
    logging_level = logging.WARN
    if verbose == 1:
        logging_level = logging.INFO
    elif verbose >= 2:
        logging_level = logging.DEBUG

    logging.basicConfig(level=logging_level, stream=sys.stderr)
    asyncio.run(serve(namespace=namespace, in_cluster=in_cluster, kubeconfig=kubeconfig))


if __name__ == "__main__":
    main()
