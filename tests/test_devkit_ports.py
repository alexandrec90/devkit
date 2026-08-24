"""Tests for the host-port registry."""

import pytest
from support import REPO_ROOT, devkit_ports, devkit_project

Registry = devkit_ports.Registry
RegistryError = devkit_ports.RegistryError
from_dict = devkit_ports.from_dict
load = devkit_ports.load
validate = devkit_ports.validate

BASE = {
    "registry": {"max_slots": 4},
    "services": {"db": 5432, "app": 8000},
    "slots": {"alpha": 0, "beta": 1},
}


def test_ports_are_the_service_base_plus_the_slot():
    registry = from_dict(BASE)
    assert registry.ports_for("alpha") == {"app": 8000, "db": 5432}
    assert registry.ports_for("beta") == {"app": 8001, "db": 5433}


def test_slot_zero_gets_the_conventional_defaults():
    # The reason slots start at 0 rather than 1: the primary checkout should be on
    # 5432/8000, so every tool that assumes a default port still works there.
    assert from_dict(BASE).ports_for("alpha")["db"] == 5432


def test_ports_for_can_be_restricted_to_the_services_a_project_uses():
    assert from_dict(BASE).ports_for("beta", ["db"]) == {"db": 5433}


def test_ports_for_rejects_an_unknown_service():
    with pytest.raises(RegistryError, match="unknown service"):
        from_dict(BASE).ports_for("alpha", ["kafka"])


def test_env_for_emits_the_host_port_names_compose_reads():
    assert from_dict(BASE).env_for("beta") == {"APP_HOST_PORT": "8001", "DB_HOST_PORT": "5433"}


def test_cli_prints_each_comma_delimited_checkout(monkeypatch, capsys):
    monkeypatch.setattr(devkit_ports, "load", lambda _root: from_dict(BASE))
    assert devkit_ports.main(["alpha,beta"]) == 0
    output = capsys.readouterr().out
    assert "[alpha]" in output
    assert "[beta]" in output
    assert "APP_HOST_PORT=8000" in output
    assert "APP_HOST_PORT=8001" in output


def test_unregistered_checkout_names_the_known_ones():
    with pytest.raises(RegistryError, match="known checkouts: alpha, beta"):
        from_dict(BASE).slot_of("gamma")


def test_next_free_slot_fills_gaps_before_extending():
    registry = from_dict({**BASE, "slots": {"alpha": 0, "beta": 2}})
    assert registry.next_free_slot() == 1


def test_next_free_slot_raises_when_the_registry_is_full():
    full = {**BASE, "slots": {f"p{i}": i for i in range(4)}}
    with pytest.raises(RegistryError, match="all 4 slots are allocated"):
        from_dict(full).next_free_slot()


def test_two_checkouts_on_one_slot_is_rejected():
    # The failure this prevents surfaces at `docker compose up` as "port is already
    # allocated", which says nothing about which other stack took it.
    with pytest.raises(RegistryError, match="slot 0 is claimed by alpha, beta"):
        from_dict({**BASE, "slots": {"alpha": 0, "beta": 0}})


def test_slot_at_or_above_max_slots_is_rejected():
    with pytest.raises(RegistryError, match=r"outside \[0, 4\)"):
        from_dict({**BASE, "slots": {"alpha": 4}})


def test_negative_slot_is_rejected():
    with pytest.raises(RegistryError, match=r"outside \[0, 4\)"):
        from_dict({**BASE, "slots": {"alpha": -1}})


def test_service_bases_closer_than_max_slots_are_rejected():
    # minio=9000 / minio_console=9001 with 16 slots is the real-world instance:
    # minio at slot 1 would publish 9001, which is the console's slot 0.
    with pytest.raises(RegistryError, match="are 1 apart, which is less than max_slots"):
        validate(16, {"minio": 9000, "minio_console": 9001}, {})


def test_service_bases_exactly_max_slots_apart_are_allowed():
    validate(4, {"a": 100, "b": 104}, {})


# --- [shared]: workspace singletons -------------------------------------------------
#
# A `[services]` base is offset by slot because each checkout runs its own copy. A
# `[shared]` port is fixed because one process serves every checkout. Conflating the
# two is not a style question: `otel_http` sat in `[services]` until 2026-08-17, which
# handed carameli 4318, sports_betting 4322 and apt-finder 4324 while exactly one
# collector existed, so two of the three exported into a closed port for a month with
# no error on either side.

SHARED = {**BASE, "shared": {"otel_http": 4318}}


def test_a_shared_port_is_the_same_for_every_checkout():
    registry = from_dict(SHARED)
    assert registry.shared_port("otel_http") == 4318
    # The point of the tier, stated as an assertion: no slot arithmetic anywhere.
    assert {registry.shared_port("otel_http") for _ in registry.slots} == {4318}


def test_a_shared_port_is_not_reachable_through_ports_for():
    # `ports_for` answers "what does this checkout publish". A singleton is published
    # by nobody in particular, and letting it leak into that answer is how it acquired
    # a per-slot offset in the first place.
    registry = from_dict(SHARED)
    assert "otel_http" not in registry.ports_for("alpha")
    with pytest.raises(RegistryError, match="unknown service"):
        registry.ports_for("alpha", ["otel_http"])


def test_shared_port_rejects_an_unknown_singleton():
    with pytest.raises(RegistryError, match="no shared port registered"):
        from_dict(SHARED).shared_port("nope")


def test_a_registry_with_no_shared_table_still_loads():
    # Every consumer predating the tier passes no `[shared]`, and must keep working.
    assert from_dict(BASE).shared == {}


def test_a_shared_port_inside_a_service_slot_range_is_rejected():
    # max_slots=4, so db owns 5432..5435. A singleton on 5434 collides only for the
    # checkout holding slot 2 -- the version of this bug that survives longest,
    # because every other checkout works.
    with pytest.raises(RegistryError, match="falls inside the slot range"):
        validate(4, {"db": 5432}, {}, {"otel_http": 5434})


def test_a_shared_port_just_past_a_slot_range_is_allowed():
    validate(4, {"db": 5432}, {}, {"otel_http": 5436})


def test_boolean_shared_port_is_not_accepted_as_an_integer():
    with pytest.raises(RegistryError, match="must be an integer"):
        from_dict({**BASE, "shared": {"otel_http": True}})


def test_shared_must_be_a_table():
    with pytest.raises(RegistryError, match=r"\[shared\] must be a table"):
        from_dict({**BASE, "shared": 4318})


def test_empty_services_is_rejected():
    with pytest.raises(RegistryError, match="nothing to allocate"):
        validate(4, {}, {})


def test_boolean_slot_is_not_accepted_as_an_integer():
    # `True == 1` in Python; without the explicit bool guard a `slot = true` typo
    # would silently allocate slot 1 and collide with whoever holds it.
    with pytest.raises(RegistryError, match="must be an integer"):
        from_dict({**BASE, "slots": {"alpha": True}})


def test_load_raises_on_a_missing_file_rather_than_defaulting(tmp_path):
    # Deliberately unlike harness_config.load(), which degrades to defaults: a
    # guessed port number is a collision, not a degraded mode.
    with pytest.raises(RegistryError, match="cannot read"):
        load(tmp_path)


def test_load_raises_on_malformed_toml(tmp_path):
    (tmp_path / "ports.toml").write_text("[services\n")
    with pytest.raises(RegistryError, match="not valid TOML"):
        load(tmp_path)


def test_the_shipped_registry_is_valid():
    registry = load(REPO_ROOT)
    assert isinstance(registry, Registry)
    assert registry.slots["carameli"] == 0


def test_the_shipped_registry_matches_the_ports_the_stacks_publish_today():
    # Backfilled, not invented: carameli publishes 5432 and ibkr_trader 5433, so
    # adopting the registry must not move either. If this fails, someone renumbered
    # a live stack — check `.env` files before changing the assertion.
    registry = load(REPO_ROOT)
    assert registry.ports_for("carameli", ["db"]) == {"db": 5432}
    assert registry.ports_for("ibkr_trader", ["db"]) == {"db": 5433}
    # carameli-b held slot 2 (db 5434) until the `-b` tier was retired. Its slot is now
    # free for an ephemeral box to lease, which is the whole point of freeing it.
    assert "carameli-b" not in registry.slots


def test_the_shipped_registry_gives_every_checkout_one_telemetry_endpoint():
    # The regression this locks: `otel_http` must not go back into `[services]`. There
    # it produced one endpoint per checkout for a collector that exists once, and the
    # projects on the wrong end of that had no way to notice -- an OTLP exporter whose
    # endpoint refuses the connection retries in the background and never reports.
    registry = load(REPO_ROOT)
    assert "otel_http" not in registry.services, (
        "otel_http is a workspace singleton; a [services] base would give every "
        "checkout its own endpoint again"
    )
    assert registry.shared_port("otel_http") == 4318


def test_no_two_shipped_checkouts_share_a_port():
    registry = load(REPO_ROOT)
    seen: dict[int, str] = {}
    for checkout in registry.slots:
        for service, port in registry.ports_for(checkout).items():
            assert port not in seen, f"{checkout}/{service} collides with {seen[port]}"
            seen[port] = f"{checkout}/{service}"


# Checkouts that deliberately hold NO slot, and why. A slot exists to offset the host
# ports a compose stack publishes; a project that publishes none needs one the way a
# library needs a port number. Registering them anyway would not be harmless — it would
# consume slots and push the next real stack further along the registry.
#
# Same bargain the picker exclusions in test_devkit_project.py strike: staying out
# stays possible, as a decision someone recorded rather than a line nobody added. The
# failure this prevents runs in both directions — a generated project whose slot
# reminder was ignored is handed the same slot as the next one, and a slotless project
# "fixed" by assigning it a slot silently renumbers nothing but wastes one forever.
SLOTLESS: dict[str, str] = {
    "devkit": (
        "no Docker stack, no database, no frontend — .devkit.toml declares "
        "[db] enabled = false and [frontend] enabled = false, which is what lets its "
        "CI run with no service containers"
    ),
    "data-lake": (
        "an installable library, not an application: it owns no engine and no "
        "migrations, its suite runs on in-memory SQLite, and it ships no compose file"
    ),
}


# NB the converse is deliberately NOT asserted. A registered checkout need not be a
# workspace folder: `sports_betting` holds slots 4-5 and is not in the `project` picker,
# and that is correct — a slot reserves the host ports a stack publishes, which it does
# whether or not the repo is open in the multi-root workspace. A test requiring
# registry ⊆ picker was written here first and deleted: it failed on sports_betting, and
# "fixing" it would have freed ports a live stack is using.


def test_every_checkout_either_holds_a_slot_or_says_why_not():
    """The other direction: generating a project prints a "register the slot" reminder,
    and nothing failed when it was skipped.

    apt-finder was generated, its worktree's `.env` was written with slot 7's ports, and
    `devkit_ports.py apt-finder` still exited 1 with "no slot registered" — so the next
    generated project would have been handed slot 6 a second time. That gap was found by
    hand, which is the part this test replaces.
    """
    registry = load(REPO_ROOT)
    canonical = devkit_project.canonical_tasks()
    picker = next(i for i in canonical["inputs"] if i["id"] == "project")
    options = picker.get("options", picker["args"]["optionGroups"][0]["options"])
    known = {o if isinstance(o, str) else o["value"] for o in options}
    missing = sorted(known - set(registry.slots) - set(SLOTLESS))
    assert not missing, (
        f"{missing} have no slot in ports.toml and no reason in SLOTLESS — either run "
        f"`python scripts/devkit_ports.py --next` and register them, or record why they "
        f"publish no host ports"
    )


def test_every_slotless_exclusion_names_a_real_checkout_and_a_reason():
    """A stale exclusion is the same bug wearing this test's uniform: it would keep one
    checkout exempt forever, and invite the next omission to be silenced by adding a
    line here rather than a slot there."""
    registry = load(REPO_ROOT)
    canonical = devkit_project.canonical_tasks()
    picker = next(i for i in canonical["inputs"] if i["id"] == "project")
    options = picker.get("options", picker["args"]["optionGroups"][0]["options"])
    known = {o if isinstance(o, str) else o["value"] for o in options}
    for name, reason in SLOTLESS.items():
        assert name in known, f"SLOTLESS names {name}, which is not a checkout"
        assert reason.strip(), f"SLOTLESS excludes {name} with no reason given"
        assert name not in registry.slots, (
            f"SLOTLESS says {name} needs no slot, but ports.toml gives it one"
        )
