#!/usr/bin/env python3
"""Reader and allocator for the host-port registry (`ports.toml` at the repo root).

Every checkout — a project or one of its `git worktree` siblings — owns one integer
**slot**, and every port it publishes is `service_base + slot`. See `ports.toml` for
why that model was chosen over per-service bookkeeping.

This module is **pure and stdlib-only** so it can be imported by the generator, by a
hook, or by a test without a virtualenv. Nothing here touches the network or the
filesystem beyond reading the registry file it is pointed at.

Unit-tested in `tests/test_devkit_ports.py`.
"""

from __future__ import annotations

import itertools
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY_NAME = "ports.toml"

# Env-var name published for each service, e.g. db -> DB_HOST_PORT. Compose files
# read these with a `${DB_HOST_PORT:-5432}` default so a bare clone still works.
ENV_SUFFIX = "_HOST_PORT"


class RegistryError(ValueError):
    """The registry file is missing, malformed, or self-inconsistent."""


@dataclass(frozen=True)
class Registry:
    """A parsed, validated `ports.toml`."""

    max_slots: int
    services: dict[str, int]
    slots: dict[str, int]
    shared: dict[str, int] = field(default_factory=dict)

    def shared_port(self, service: str) -> int:
        """The fixed port of a workspace singleton (see `[shared]` in the registry).

        Deliberately *not* reachable through `ports_for`: that method answers "what
        does this checkout publish", and a singleton is not published per checkout.
        Folding the two together is what produced a per-project telemetry endpoint
        for a collector that only ever existed once.
        """
        try:
            return self.shared[service]
        except KeyError:
            known = ", ".join(sorted(self.shared)) or "(none)"
            raise RegistryError(
                f"no shared port registered for {service!r}; known: {known}. "
                f"Add it to [shared] in {REGISTRY_NAME}."
            ) from None

    def slot_of(self, checkout: str) -> int:
        """The slot registered for `checkout` (a directory name)."""
        try:
            return self.slots[checkout]
        except KeyError:
            known = ", ".join(sorted(self.slots)) or "(none)"
            raise RegistryError(
                f"no slot registered for {checkout!r}; known checkouts: {known}. "
                f"Add it to [slots] in {REGISTRY_NAME}."
            ) from None

    def ports_for_slot(self, slot: int, services: list[str] | None = None) -> dict[str, int]:
        """`{service: host_port}` for an explicit slot.

        Separate from `ports_for` because the generator resolves a slot for a project
        that is not in `[slots]` yet (it falls back to `next_free_slot`), and must
        then price that slot rather than look the unregistered name up again.
        """
        wanted = services if services is not None else sorted(self.services)
        unknown = [s for s in wanted if s not in self.services]
        if unknown:
            raise RegistryError(
                f"unknown service(s) {', '.join(unknown)}; "
                f"known: {', '.join(sorted(self.services))}"
            )
        return {s: self.services[s] + slot for s in wanted}

    def ports_for(self, checkout: str, services: list[str] | None = None) -> dict[str, int]:
        """`{service: host_port}` for one registered checkout."""
        return self.ports_for_slot(self.slot_of(checkout), services)

    def env_for_slot(self, slot: int, services: list[str] | None = None) -> dict[str, str]:
        """`ports_for_slot` as the `*_HOST_PORT` env pairs a compose file reads."""
        return {
            f"{s.upper()}{ENV_SUFFIX}": str(port)
            for s, port in self.ports_for_slot(slot, services).items()
        }

    def env_for(self, checkout: str, services: list[str] | None = None) -> dict[str, str]:
        """`ports_for` as the `*_HOST_PORT` env pairs a compose file reads."""
        return self.env_for_slot(self.slot_of(checkout), services)

    def next_free_slot(self) -> int:
        """The lowest slot not yet taken. Raises when the registry is full."""
        taken = set(self.slots.values())
        for candidate in range(self.max_slots):
            if candidate not in taken:
                return candidate
        raise RegistryError(
            f"all {self.max_slots} slots are allocated; raise max_slots in "
            f"{REGISTRY_NAME} (and re-check service-base spacing) to add more."
        )


def validate(
    max_slots: int,
    services: dict[str, int],
    slots: dict[str, int],
    shared: dict[str, int] | None = None,
) -> None:
    """Raise `RegistryError` unless the registry is internally consistent.

    Four ways it can be wrong, all of which produce a port collision at `docker
    compose up` that reads as "port is already allocated" with no hint of the cause:

    1. two checkouts sharing a slot — their whole stacks overlap;
    2. a slot outside `[0, max_slots)` — unvalidated spacing below it;
    3. two service bases closer together than `max_slots` — a high slot of the lower
       service lands on a low slot of the higher one;
    4. a `[shared]` singleton inside some service's slot range — the singleton is a
       fixed number and the slot port moves, so this collides only for the one
       checkout holding that slot, which is the hardest version of this bug to see.
    """
    if max_slots < 1:
        raise RegistryError(f"registry.max_slots must be >= 1, got {max_slots}")
    if not services:
        raise RegistryError("[services] is empty; nothing to allocate")

    by_slot: dict[int, list[str]] = {}
    for checkout, slot in slots.items():
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise RegistryError(f"slot for {checkout!r} must be an integer, got {slot!r}")
        if not 0 <= slot < max_slots:
            raise RegistryError(
                f"slot {slot} for {checkout!r} is outside [0, {max_slots}); "
                f"raise registry.max_slots or pick a free slot"
            )
        by_slot.setdefault(slot, []).append(checkout)
    for slot, holders in sorted(by_slot.items()):
        if len(holders) > 1:
            raise RegistryError(
                f"slot {slot} is claimed by {', '.join(sorted(holders))}; "
                f"each checkout needs its own slot"
            )

    ordered = sorted(services.items(), key=lambda kv: kv[1])
    for (lo_name, lo), (hi_name, hi) in itertools.pairwise(ordered):
        if hi - lo < max_slots:
            raise RegistryError(
                f"service bases {lo_name}={lo} and {hi_name}={hi} are {hi - lo} apart, "
                f"which is less than max_slots={max_slots}: slot {max_slots - 1} of "
                f"{lo_name} would collide with an early slot of {hi_name}. "
                f"Move one base at least {max_slots} away."
            )

    for name, port in sorted((shared or {}).items()):
        if not isinstance(port, int) or isinstance(port, bool):
            raise RegistryError(f"shared port for {name!r} must be an integer, got {port!r}")
        for service, base in sorted(services.items(), key=lambda kv: kv[1]):
            if base <= port < base + max_slots:
                raise RegistryError(
                    f"shared port {name}={port} falls inside the slot range of service "
                    f"base {service}={base} ([{base}, {base + max_slots})): the checkout "
                    f"on slot {port - base} would publish {service} on the singleton's "
                    f"port. Move one of them."
                )


def from_dict(data: dict) -> Registry:
    """Build a validated `Registry` from an already-parsed registry mapping."""
    registry = data.get("registry", {})
    services = data.get("services", {})
    slots = data.get("slots", {})
    shared = data.get("shared", {})
    if not isinstance(services, dict) or not isinstance(slots, dict):
        raise RegistryError("[services] and [slots] must both be tables")
    if not isinstance(shared, dict):
        raise RegistryError("[shared] must be a table")
    max_slots = registry.get("max_slots", 16) if isinstance(registry, dict) else 16
    validate(max_slots, services, slots, shared)
    return Registry(
        max_slots=max_slots,
        services=dict(services),
        slots=dict(slots),
        shared=dict(shared),
    )


def load(root: Path) -> Registry:
    """Load and validate `<root>/ports.toml`.

    Unlike `harness_config.load`, this **raises** on a bad file instead of falling
    back to defaults. That module feeds a Stop hook, where a config typo must never
    break the session; this one hands out port numbers, where guessing silently is
    exactly how two stacks end up on the same port.
    """
    path = root / REGISTRY_NAME
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path} is not valid TOML: {exc}") from exc
    return from_dict(data)


def main(argv: list[str] | None = None) -> int:
    """Print the registry, or env blocks for comma-delimited checkout selections."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent.parent
    try:
        registry = load(root)
        if not args:
            print(f"slots (max {registry.max_slots}):")
            for checkout, slot in sorted(registry.slots.items(), key=lambda kv: kv[1]):
                print(f"  {slot:>2}  {checkout}")
            print("service bases:")
            for service, base in sorted(registry.services.items(), key=lambda kv: kv[1]):
                print(f"      {service} = {base}")
            if registry.shared:
                print("shared (one per workspace, not offset by slot):")
                for service, port in sorted(registry.shared.items(), key=lambda kv: kv[1]):
                    print(f"      {service} = {port}")
            return 0
        selected = list(
            dict.fromkeys(
                checkout.strip()
                for value in args
                for checkout in value.split(",")
                if checkout.strip()
            )
        )
        for index, checkout in enumerate(selected):
            if len(selected) > 1:
                if index:
                    print()
                print(f"[{checkout}]")
            for name, value in registry.env_for(checkout).items():
                print(f"{name}={value}")
    except RegistryError as exc:
        print(f"devkit-ports: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
