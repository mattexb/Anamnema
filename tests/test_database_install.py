"""Fresh installs only, configuration before the first start, secrets never in argv, and a
replay that resumes after the last finished step without re-initializing anything."""

from pathlib import Path
from typing import Any

import pytest

from anamnema.database_install import (
    MARKER,
    DatabaseInstaller,
    DatabaseTargets,
    Ran,
    database_policy,
    neo4j_config,
)
from anamnema.setup_config import SetupError

FINGERPRINT = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
DEFAULT_PASSWORD = database_policy().neo4j.default_password
SHIPPED = "#server.default_listen_address=0.0.0.0\nserver.memory.heap.max_size=1g\n"


class Host:
    """A fake Debian host: records every command, answers like the real tools would."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.installed: set[str] = set()
        self.paths: set[str] = set()
        self.data = False
        self.fail: set[str] = set()
        self.neo4j_password = DEFAULT_PASSWORD
        self.listen_after = 0
        self.probes = 0

    def __call__(
        self, argv: list[str], stdin: bytes | None, timeout: float, env: dict[str, str] | None
    ) -> Ran:
        self.calls.append({"argv": argv, "stdin": stdin or b"", "env": env or {}})
        verb = next((a for a in argv if a not in ("sudo", "-n", "env")), "")
        if any(tag in " ".join(argv) for tag in self.fail):
            return Ran(1, b"")
        if verb == "curl":
            return Ran(0, b"-----BEGIN PGP PUBLIC KEY BLOCK-----")
        if argv[:2] == ["gpg", "--show-keys"]:
            return Ran(0, f"pub:x\nfpr:::::::::{FINGERPRINT}:\n".encode())
        if argv[:2] == ["gpg", "--dearmor"]:
            return Ran(0, b"binary-key")
        if verb == "install":
            self.paths.add(argv[-1])
        if "DEBIAN_FRONTEND=noninteractive" in argv:
            self.installed |= {a.split("=")[0] for a in argv if a.startswith(("mongodb", "neo4j"))}
        if verb == "cat":
            return Ran(0, SHIPPED.encode())
        if verb == "mongosh":
            return Ran(0, f"{MARKER}\n".encode())
        if verb == "cypher-shell":
            given = (env or {}).get("NEO4J_PASSWORD")
            query = (stdin or b"").decode()
            if given != self.neo4j_password:
                return Ran(1, b"")
            if query.startswith("ALTER CURRENT USER"):
                self.neo4j_password = query.split("TO '", 1)[1].split("'", 1)[0]
            return Ran(0, f"{MARKER}\n".encode())
        return Ran(0, b"")

    def package_installed(self, name: str) -> bool:
        return name in self.installed

    def exists(self, path: Path) -> bool:
        return str(path) in self.paths

    def nonempty_directory(self, path: Path) -> bool:
        return self.data

    def listening(self, host: str, port: int) -> bool:
        self.probes += 1
        return host == "127.0.0.1" and self.probes > self.listen_after


class Vault:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.order: list[str] = []

    def lookup(self, entry: str, field: str) -> str:
        if (entry, field) not in self.values:
            raise SetupError("absent")
        return self.values[(entry, field)]

    def store(self, entry: str, field: str, value: str) -> None:
        self.values[(entry, field)] = value
        self.order.append(f"{entry}:{field}")


TARGETS = DatabaseTargets(
    mongo_port=27017,
    neo4j_port=7687,
    archive_database="anamnema",
    archive_entry="archive/mongodb",
    archive_field="uri",
    graph_entry="graph/neo4j",
    graph_user_field="user",
    graph_password_field="password",  # noqa: S106 - keyring field name, not a secret
)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[DatabaseInstaller, Host, Vault]:
    policy = database_policy()
    policy = policy.model_copy(
        update={
            "mongodb": policy.mongodb.model_copy(update={"key_fingerprint": FINGERPRINT}),
            "neo4j": policy.neo4j.model_copy(update={"key_fingerprint": FINGERPRINT}),
        }
    )
    host, vault = Host(), Vault()
    return DatabaseInstaller(policy, TARGETS, host, host, vault, tmp_path / "state"), host, vault


def _argv_text(host: Host) -> str:
    return " ".join(" ".join(call["argv"]) for call in host.calls)


def test_mongodb_installs_configured_first_with_starts_held_and_no_secret_in_argv(setup):
    installer, host, vault = setup
    assert installer.plan()["mongodb"]["state"] == "absent"
    result = installer.apply("mongodb")
    assert result["complete"] is True
    order = [" ".join(c["argv"][2:]) for c in host.calls]
    config_at = next(i for i, o in enumerate(order) if o.endswith("/etc/mongod.conf"))
    mask_at = order.index("systemctl mask mongod.service")
    install_at = next(i for i, o in enumerate(order) if "apt-get install" in o)
    unmask_at = order.index("systemctl unmask mongod.service")
    start_at = order.index("systemctl enable --now mongod.service")
    assert config_at < mask_at < install_at < unmask_at < start_at
    config = next(c["stdin"] for c in host.calls if c["argv"][-1] == "/etc/mongod.conf")
    assert b"bindIp: 127.0.0.1" in config and b"authorization: enabled" in config
    install = next(c["argv"] for c in host.calls if "DEBIAN_FRONTEND=noninteractive" in c["argv"])
    assert "Dpkg::Options::=--force-confold" in install
    for value in vault.values.values():
        assert value.split("@")[0].split(":")[-1] not in _argv_text(host)
    assert vault.order == ["archive/mongodb-admin:password", "archive/mongodb:uri"]
    uri = vault.values[("archive/mongodb", "uri")]
    assert uri.startswith("mongodb://anamnema:") and uri.endswith("@127.0.0.1:27017/anamnema")
    assert installer.plan()["mongodb"] == {
        "state": "managed",
        "steps": [],
        "complete": True,
        "keyring_entries": ["archive/mongodb:uri", "archive/mongodb-admin:password"],
    }


def test_an_interrupted_bootstrap_resumes_there_with_the_same_secrets(setup):
    installer, host, vault = setup
    host.fail = {"mongosh"}
    with pytest.raises(SetupError, match="bootstrap failed"):
        installer.apply("mongodb")
    stored = dict(vault.values)
    host.fail = set()
    before = len(host.calls)
    assert installer.apply("mongodb")["complete"] is True
    resumed = [c["argv"][0] for c in host.calls[before:]]
    assert resumed == ["mongosh", "mongosh"]
    assert vault.values == stored


def test_an_existing_unmanaged_install_or_data_is_refused_untouched(setup):
    installer, host, _vault = setup
    host.data = True
    assert installer.plan()["mongodb"]["state"] == "foreign"
    with pytest.raises(SetupError, match="refused untouched"):
        installer.apply("mongodb")
    assert host.calls == []


def test_an_unpinned_or_different_signing_key_is_refused(setup, tmp_path):
    installer, host, _vault = setup
    installer.policy = installer.policy.model_copy(
        update={"mongodb": installer.policy.mongodb.model_copy(update={"key_fingerprint": "00"})}
    )
    with pytest.raises(SetupError, match="differs from the pinned"):
        installer.apply("mongodb")
    shipped = database_policy()
    empty = shipped.model_copy(
        update={"mongodb": shipped.mongodb.model_copy(update={"key_fingerprint": ""})}
    )
    unpinned = DatabaseInstaller(empty, TARGETS, host, host, setup[2], tmp_path / "s2")
    with pytest.raises(SetupError, match="not pinned"):
        unpinned.apply("mongodb")


def test_neo4j_first_login_changes_the_default_password_on_stdin_only(setup):
    installer, host, vault = setup
    assert installer.apply("neo4j")["complete"] is True
    password = vault.values[("graph/neo4j", "password")]
    assert vault.values[("graph/neo4j", "user")] == "neo4j"
    assert host.neo4j_password == password
    assert password not in _argv_text(host)
    changes = [c for c in host.calls if c["stdin"].startswith(b"ALTER CURRENT USER")]
    assert len(changes) == 1 and changes[0]["env"]["NEO4J_PASSWORD"] == DEFAULT_PASSWORD
    written = next(
        c["stdin"]
        for c in host.calls
        if c["argv"][-1] == "/etc/neo4j/neo4j.conf" and "install" in c["argv"]
    )
    assert b"server.bolt.listen_address=127.0.0.1:7687" in written
    assert b"server.memory.heap.max_size=1g" in written and b"0.0.0.0" not in written


def test_a_replayed_neo4j_bootstrap_does_not_change_the_password_twice(setup):
    installer, host, vault = setup
    host.fail = {"neo4j.service"}
    with pytest.raises(SetupError):
        installer.apply("neo4j")
    host.fail = set()
    installer.apply("neo4j")
    host.calls.clear()
    installer._neo4j_bootstrap()  # noqa: SLF001 - replay of an already applied step
    assert not any(c["stdin"].startswith(b"ALTER") for c in host.calls)
    assert host.neo4j_password == vault.values[("graph/neo4j", "password")]


def test_neo4j_config_keeps_other_settings_and_replaces_the_listener():
    text = neo4j_config(SHIPPED + "server.http.enabled=true\n", TARGETS)
    assert "server.http.enabled=true" not in text and "server.http.enabled=false" in text
    assert text.count("server.default_listen_address=") == 1


def test_a_receipt_made_for_other_settings_is_refused(setup, tmp_path):
    installer, host, vault = setup
    installer.apply("mongodb")
    moved = TARGETS.model_copy(update={"mongo_port": 27018})
    other = DatabaseInstaller(installer.policy, moved, host, host, vault, installer.state)
    host.calls.clear()
    with pytest.raises(SetupError, match="other settings"):
        other.apply("mongodb")
    assert host.calls == []


def test_a_completed_install_verifies_the_login_again_on_replay(setup):
    installer, host, _vault = setup
    installer.apply("mongodb")
    host.calls.clear()
    assert installer.apply("mongodb")["complete"] is True
    assert [c["argv"][0] for c in host.calls] == ["mongosh"]
    host.fail = {"mongosh"}
    with pytest.raises(SetupError, match="login check failed"):
        installer.apply("mongodb")


def test_a_lookup_failure_after_the_secrets_step_never_regenerates(setup):
    installer, host, vault = setup
    host.fail = {"mongosh"}
    with pytest.raises(SetupError):
        installer.apply("mongodb")
    stored = dict(vault.values)
    vault.values.clear()  # a locked keyring answers like a missing entry
    host.fail = set()
    with pytest.raises(SetupError, match="never regenerated"):
        installer.apply("mongodb")
    assert vault.order.count("archive/mongodb:uri") == 1 and stored


def test_an_admin_unit_override_counts_as_an_existing_install(setup):
    installer, host, _vault = setup
    host.paths.add("/etc/systemd/system/mongod.service")
    with pytest.raises(SetupError, match="refused untouched"):
        installer.apply("mongodb")


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://someone:pw@127.0.0.1:27017/anamnema",
        "mongodb://anamnema:pw@127.0.0.1:27018/anamnema",
        "mongodb://anamnema:pw@127.0.0.1:27017/other",
        "mongodb://anamnema:pw@127.0.0.1:27017/anamnema?authSource=admin",
        "mongodb://anamnema:pw@10.0.0.5:27017/anamnema",
    ],
)
def test_a_stored_uri_for_another_target_is_refused(setup, uri):
    installer, _host, vault = setup
    vault.values[("archive/mongodb", "uri")] = uri
    with pytest.raises(SetupError, match="another user, host, port or database"):
        installer.apply("mongodb")


def test_a_percent_encoded_stored_password_is_decoded_by_the_parser(setup):
    installer, host, vault = setup
    vault.values[("archive/mongodb", "uri")] = (
        "mongodb://anamnema:a%2Fb%40c@127.0.0.1:27017/anamnema"
    )
    installer.apply("mongodb")
    scripts = [c["stdin"].decode() for c in host.calls if c["argv"][0] == "mongosh"]
    assert '"q": "a/b@c"' in scripts[0]


def test_mongosh_reads_its_script_as_one_file_with_no_statement_split_across_lines(setup):
    installer, host, _vault = setup
    installer.apply("mongodb")
    calls = [c for c in host.calls if c["argv"][0] == "mongosh"]
    assert calls and all(c["argv"][-2:] == ["--file", "/dev/stdin"] for c in calls)
    for call in calls:
        lines = call["stdin"].decode().splitlines()
        assert not any(line.lstrip().startswith("else") for line in lines)


def test_provenance_text_is_not_bound_but_every_pin_is(setup, tmp_path):
    installer, host, vault = setup
    policy = installer.policy

    def other(**source: Any) -> DatabaseInstaller:
        changed = policy.model_copy(update={"mongodb": policy.mongodb.model_copy(update=source)})
        return DatabaseInstaller(changed, TARGETS, host, host, vault, tmp_path / "x")

    assert other(key_provenance="another description").inputs == installer.inputs
    assert other(key_fingerprint="00" * 20).inputs != installer.inputs
    assert other(key_url="https://example.invalid/k.asc").inputs != installer.inputs


def test_bootstrap_waits_for_the_loopback_listener_after_start(setup):
    installer, host, _vault = setup
    host.listen_after = 3
    slept: list[float] = []
    installer.sleep = slept.append
    assert installer.apply("neo4j")["complete"] is True
    assert len(slept) == 3 and host.neo4j_password != DEFAULT_PASSWORD


def test_a_listener_that_never_opens_stops_at_start_with_the_receipt_kept(setup):
    installer, host, _vault = setup
    host.listen_after = 10**9
    ticks = iter(range(0, 10**6, 30))
    installer.clock = lambda: float(next(ticks))
    installer.sleep = lambda _s: None
    with pytest.raises(SetupError, match="is not listening"):
        installer.apply("neo4j")
    assert installer.plan()["neo4j"]["steps"][0] == "start"
    assert not any(c["argv"][0] == "cypher-shell" for c in host.calls)


def test_timing_bounds_are_not_bound_inputs(setup, tmp_path):
    installer, host, vault = setup
    slower = installer.policy.model_copy(update={"ready_wait_seconds": 999.0})
    other = DatabaseInstaller(slower, TARGETS, host, host, vault, tmp_path / "y")
    assert other.inputs == installer.inputs
