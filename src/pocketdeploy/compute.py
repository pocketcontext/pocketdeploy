"""Compute provider boundary; provider objects do not authenticate until used.

Providers retain their own cloud identities, resource graph and durable intents.
The controller consumes only safe observations and normalized lifecycle states.
"""
from typing import Protocol

from .common import DeployError


class ComputeProvider(Protocol):
    resource_kinds: frozenset[str]
    delete_pending_key: str

    def inspect(self) -> dict: ...
    def plan(self) -> list: ...
    def converge(self, public_key: str, operation_id: str) -> dict: ...
    def connection(self) -> dict: ...
    def plan_delete(self) -> list: ...
    def delete(self, operation_id: str) -> dict: ...
    def adopt(self, instance_id: str, operation_id: str) -> dict: ...


def provider_class(name):
    if name == 'oci':
        from .oci import OCI
        return OCI
    if name == 'digitalocean':
        from .digitalocean import DigitalOcean
        return DigitalOcean
    if name == 'gcp':
        from .gcp import GCP
        return GCP
    raise DeployError('Unsupported compute provider.')
