"""Install MongoDB and Neo4j from their official Debian 13 repositories for one local user.

``plan`` is read-only. ``apply`` installs fresh databases only: a package, configuration or
data directory already present without this installer's receipt is refused untouched. Each
database's own configuration is written before its package installs, and service starts are
held during the install, so the first start is already loopback-only with authentication on.
Credentials are generated and stored in the logged-in user's Secret Service before any
bootstrap and reach a process only on stdin or in its environment, never in argv, output or a
file. Every step is journalled before it runs and is safe to repeat: a replay reconciles what
an interrupted step already did (an existing secret is reused, an existing user is verified with
the stored credential) and never reruns a destructive initialization. Data is never removed;
this module has no remove step by design.
"""

import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote

from pydantic import Field, ValidationError
from pymongo.uri_parser import parse_uri

from anamnema.archive_models import digest
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import Nonempty, SetupError, StrictModel

Step = Literal["secrets", "repository", "config", "package", "start", "bootstrap", "verify"]
STEPS: tuple[Step, ...] = (
    "secrets",
    "repository",
    "config",
    "package",
    "start",
    "bootstrap",
    "verify",
)
MARKER = "anamnema-database-ok"
SYSTEM_UNITS = Path("/etc/systemd/system")


class Source(StrictModel):
    key_url: Nonempty
    key_fingerprint: str
    key_provenance: str = ""
    keyring: Path
    source_file: Path
    source_line: Nonempty
    packages: tuple[Nonempty, ...]
    package_probe: Nonempty
    config_file: Path
    data_directory: Path
    service: Nonempty
    shell: Nonempty


class MongoSource(Source):
    admin_entry: Nonempty
    admin_user: Nonempty
    application_user: Nonempty


class Neo4jSource(Source):
    default_user: Nonempty
    default_password: Nonempty


class DatabasePolicy(StrictModel):
    privileged: tuple[Nonempty, ...]
    command_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_output_bytes: int = Field(gt=0)
    max_receipt_bytes: int = Field(gt=0)
    secret_bytes: int = Field(ge=24)
    ready_wait_seconds: float = Field(gt=0, allow_inf_nan=False)
    ready_poll_seconds: float = Field(gt=0, allow_inf_nan=False)
    mongodb: MongoSource
    neo4j: Neo4jSource


class DatabaseTargets(StrictModel):
    """Bound by the installer to the actual setup, archive and memory settings."""

    mongo_port: int = Field(gt=0, lt=65536)
    neo4j_port: int = Field(gt=0, lt=65536)
    archive_database: Nonempty
    archive_entry: Nonempty
    archive_field: Nonempty
    graph_entry: Nonempty
    graph_user_field: Nonempty
    graph_password_field: Nonempty


@dataclass(frozen=True)
class Ran:
    code: int
    stdout: bytes


class Runner(Protocol):
    def __call__(
        self, argv: list[str], stdin: bytes | None, timeout: float, env: dict[str, str] | None
    ) -> Ran: ...


class Probe(Protocol):
    def package_installed(self, name: str) -> bool: ...
    def exists(self, path: Path) -> bool: ...
    def nonempty_directory(self, path: Path) -> bool: ...
    def listening(self, host: str, port: int) -> bool: ...


class Secrets(Protocol):
    def lookup(self, entry: str, field: str) -> str: ...
    def store(self, entry: str, field: str, value: str) -> None: ...


class Receipt(StrictModel):
    version: Literal[1]
    database: Literal["mongodb", "neo4j"]
    inputs: Nonempty
    done: tuple[Step, ...]


def database_policy(path: Path | None = None) -> DatabasePolicy:
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/debian_databases.json")
            .read_text(encoding="utf-8")
        )
        return DatabasePolicy.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid database policy ({type(exc).__name__})") from exc


def mongo_config(policy: DatabasePolicy, targets: DatabaseTargets) -> str:
    """Loopback only, authorization on from the very first start."""
    return "\n".join(
        [
            "storage:",
            f"  dbPath: {policy.mongodb.data_directory}",
            "systemLog:",
            "  destination: file",
            "  logAppend: true",
            "  path: /var/log/mongodb/mongod.log",
            "net:",
            f"  port: {targets.mongo_port}",
            "  bindIp: 127.0.0.1",
            "security:",
            "  authorization: enabled",
            "processManagement:",
            "  timeZoneInfo: /usr/share/zoneinfo",
            "",
        ]
    )


NEO4J_SETTINGS = (
    "server.default_listen_address",
    "server.bolt.listen_address",
    "server.http.enabled",
    "server.https.enabled",
)


def neo4j_config(shipped: str, targets: DatabaseTargets) -> str:
    """The shipped file with only the listener settings replaced: loopback Bolt, no HTTP."""
    wanted = {
        "server.default_listen_address": "127.0.0.1",
        "server.bolt.listen_address": f"127.0.0.1:{targets.neo4j_port}",
        "server.http.enabled": "false",
        "server.https.enabled": "false",
    }
    kept = [
        line
        for line in shipped.splitlines()
        if line.split("=", 1)[0].strip().lstrip("#").strip() not in wanted
    ]
    return (
        "\n".join([*kept, "# Anamnema: loopback only", *(f"{k}={v}" for k, v in wanted.items())])
        + "\n"
    )


class DatabaseInstaller:
    """One database at a time, each with its own receipt under the installer's state."""

    def __init__(
        self,
        policy: DatabasePolicy,
        targets: DatabaseTargets,
        runner: Runner,
        probe: Probe,
        secret_store: Secrets,
        state_directory: Path,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.policy, self.targets, self.runner = policy, targets, runner
        self.probe, self.secrets, self.state = probe, secret_store, state_directory
        self.files = PrivateFiles(0o700, 0o600, ".databases-")
        # key_provenance only describes where a pin came from; it changes nothing executed,
        # so it is left out of the bound inputs (receipts made before it existed still match).
        descriptive = {"key_provenance"}
        self.inputs = digest(
            {
                "policy": policy.model_dump(
                    mode="json",
                    exclude={
                        "mongodb": descriptive,
                        "neo4j": descriptive,
                        "ready_wait_seconds": True,
                        "ready_poll_seconds": True,
                    },
                ),
                "targets": targets.model_dump(mode="json"),
                "uid": os.getuid(),
            }
        )
        self._done: list[Step] = []
        self.clock, self.sleep = clock, sleep

    def plan(self) -> dict[str, object]:
        """Read-only: for each database, absent, managed or foreign, and what apply would run."""
        report: dict[str, object] = {}
        for name in ("mongodb", "neo4j"):
            receipt = self._load(name)
            state = "managed" if receipt else ("foreign" if self._foreign(name) else "absent")
            done = receipt.done if receipt else ()
            report[name] = {
                "state": state,
                "steps": [step for step in STEPS if step not in done] if state != "foreign" else [],
                "complete": "verify" in done,
                "keyring_entries": self._entries(name),
            }
        return report

    def apply(self, name: Literal["mongodb", "neo4j"]) -> dict[str, object]:
        """Intent is saved before the first external effect; the login check always runs now."""
        self.files.directory(self.state)
        with self.files.locked(self.state / f".{name}.lock"):
            receipt = self._load(name)
            if receipt is None:
                if self._foreign(name):
                    raise SetupError(f"{name} is already installed or has data; refused untouched")
                self._save(name, [])
            elif receipt.inputs != self.inputs:
                raise SetupError(f"{name} receipt was made for other settings; nothing changed")
            self._done = list(receipt.done) if receipt else []
            for step in STEPS:
                if step in self._done and step != "verify":
                    continue
                getattr(self, f"_{name}_{step}")()
                if step not in self._done:
                    self._done.append(step)
                self._save(name, self._done)
            return {"database": name, "complete": True, "done": list(self._done)}

    # Shared steps -------------------------------------------------------------------------

    def _foreign(self, name: str) -> bool:
        """A package, configuration, data or an admin's unit override (a mask included)."""
        source: Source = getattr(self.policy, name)
        return (
            self.probe.package_installed(source.package_probe)
            or self.probe.exists(source.config_file)
            or self.probe.nonempty_directory(source.data_directory)
            or self.probe.exists(SYSTEM_UNITS / source.service)
        )

    def _entries(self, name: str) -> list[str]:
        t = self.targets
        if name == "mongodb":
            return [
                f"{t.archive_entry}:{t.archive_field}",
                f"{self.policy.mongodb.admin_entry}:password",
            ]
        return [
            f"{t.graph_entry}:{t.graph_user_field}",
            f"{t.graph_entry}:{t.graph_password_field}",
        ]

    def _secret(self, entry: str, field: str, make: str | None = None) -> str:
        """Reuse a stored value. A new one is generated only while the secrets step has not
        finished, which is before any database exists: a lookup failure after that (a locked
        or unreachable keyring looks the same as a missing entry) stops, never regenerates."""
        try:
            return self.secrets.lookup(entry, field)
        except SetupError:
            if "secrets" in self._done:
                raise SetupError(
                    f"stored credential {entry}:{field} could not be read; unlock the keyring "
                    "and retry; it is never regenerated once a database may use it"
                ) from None
            value = make if make is not None else secrets.token_urlsafe(self.policy.secret_bytes)
            self.secrets.store(entry, field, value)
            return value

    def _root(self, argv: list[str], stdin: bytes | None = None) -> Ran:
        return self._run([*self.policy.privileged, *argv], stdin)

    def _run(
        self, argv: list[str], stdin: bytes | None = None, env: dict[str, str] | None = None
    ) -> Ran:
        result = self.runner(argv, stdin, self.policy.command_seconds, env)
        if len(result.stdout) > self.policy.max_output_bytes:
            raise SetupError("database command output exceeds configured bound")
        return result

    def _must(self, result: Ran, what: str) -> Ran:
        if result.code:
            raise SetupError(f"{what} failed (exit {result.code}); receipt kept for replay")
        return result

    def _write(self, path: Path, content: bytes, mode: str = "0644") -> None:
        self._must(self._root(["install", "-m", mode, "/dev/stdin", str(path)], content), "write")

    def _repository(self, source: Source) -> None:
        if not source.key_fingerprint:
            raise SetupError("repository key fingerprint is not pinned in the policy")
        key = self._must(self._run(["curl", "-fsSL", source.key_url]), "key download").stdout
        shown = self._must(
            self._run(["gpg", "--show-keys", "--with-colons", "/dev/stdin"], key), "key check"
        ).stdout.decode("utf-8", "replace")
        prints = {line.split(":")[9] for line in shown.splitlines() if line.startswith("fpr:")}
        if source.key_fingerprint.replace(" ", "").upper() not in prints:
            raise SetupError("repository key fingerprint differs from the pinned one")
        armored = self._must(self._run(["gpg", "--dearmor"], key), "key dearmor").stdout
        self._write(source.keyring, armored)
        self._write(source.source_file, (source.source_line + "\n").encode("utf-8"))

    def _package(self, source: Source) -> None:
        """The unit is masked before its package installs, so nothing the package runs can
        start it; the mask is this unit's only and is lifted in the start step, after our
        configuration is in place. Masking again on replay changes nothing."""
        self._must(self._root(["systemctl", "mask", source.service]), "service hold")
        self._must(self._root(["apt-get", "update"]), "apt-get update")
        self._must(
            self._root(
                [
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "apt-get",
                    "install",
                    "-y",
                    "-o",
                    "Dpkg::Options::=--force-confold",
                    *source.packages,
                ]
            ),
            "package install",
        )

    def _start(self, source: Source, port: int) -> None:
        """Start, then wait for the loopback listener before any bootstrap logs in: a JVM opens
        Bolt seconds after systemd reports the start (Neo4j's first bootstrap failed on VM1015).
        Readiness is the open port, never a login retried with a possibly wrong password."""
        self._must(self._root(["systemctl", "unmask", source.service]), "service release")
        self._must(self._root(["systemctl", "enable", "--now", source.service]), "service start")
        deadline = self.clock() + self.policy.ready_wait_seconds
        while not self.probe.listening("127.0.0.1", port):
            if self.clock() >= deadline:
                raise SetupError(
                    f"{source.service} started but 127.0.0.1:{port} is not listening after "
                    f"{self.policy.ready_wait_seconds:g} s; receipt kept for replay"
                )
            self.sleep(self.policy.ready_poll_seconds)

    # MongoDB --------------------------------------------------------------------------------

    def _mongodb_secrets(self) -> None:
        self._mongo_credentials()

    def _mongo_credentials(self) -> tuple[str, str]:
        t, m = self.targets, self.policy.mongodb
        admin = self._secret(m.admin_entry, "password")
        try:
            uri = self.secrets.lookup(t.archive_entry, t.archive_field)
        except SetupError:
            uri = self._secret(t.archive_entry, t.archive_field, self._new_uri())
        return admin, self._uri_password(uri)

    def _new_uri(self) -> str:
        t = self.targets
        user = quote(self.policy.mongodb.application_user, safe="")
        password = quote(secrets.token_urlsafe(self.policy.secret_bytes), safe="")
        return f"mongodb://{user}:{password}@127.0.0.1:{t.mongo_port}/{t.archive_database}"

    def _uri_password(self, uri: str) -> str:
        """Parse with pymongo's own parser and require exactly the targets the clients use."""
        t = self.targets
        try:
            parsed = parse_uri(uri)
        except (ValueError, TypeError) as exc:
            raise SetupError("stored archive URI cannot be parsed") from exc
        options = {str(key).lower(): value for key, value in parsed["options"].items()}
        source = options.get("authsource") or parsed["database"]
        if (
            parsed["username"] != self.policy.mongodb.application_user
            or parsed["nodelist"] != [("127.0.0.1", t.mongo_port)]
            or parsed["database"] != t.archive_database
            or source != t.archive_database
            or not parsed["password"]
        ):
            raise SetupError("stored archive URI names another user, host, port or database")
        return str(parsed["password"])

    def _mongodb_repository(self) -> None:
        self._repository(self.policy.mongodb)

    def _mongodb_config(self) -> None:
        self._write(
            self.policy.mongodb.config_file, mongo_config(self.policy, self.targets).encode()
        )

    def _mongodb_package(self) -> None:
        self._package(self.policy.mongodb)

    def _mongodb_start(self) -> None:
        self._start(self.policy.mongodb, self.targets.mongo_port)

    def _mongosh(self, script: str) -> Ran:
        url = f"mongodb://127.0.0.1:{self.targets.mongo_port}/admin"
        # Script mode: stdin read as one file, not a REPL fed line by line (a REPL runs each
        # complete statement as it arrives, so a following `else` was a syntax error, and it
        # echoes the credential-bearing source; found natively on VM1015 by Elian).
        argv = [self.policy.mongodb.shell, "--quiet", "--norc", url, "--file", "/dev/stdin"]
        return self._run(argv, script.encode())

    def _mongodb_bootstrap(self) -> None:
        """Create the admin through the localhost exception only when its login fails; an
        existing admin with another password is refused, never reinitialized."""
        admin, application = self._mongo_credentials()
        m, t = self.policy.mongodb, self.targets
        values = json.dumps(
            {
                "u": m.admin_user,
                "p": admin,
                "a": m.application_user,
                "q": application,
                "d": t.archive_database,
            }
        )
        script = (
            f"const v = {values};\nconst adm = db.getSiblingDB('admin');\nlet ok = false;\n"
            "try { ok = Boolean(adm.auth(v.u, v.p)); } catch (e) { ok = false; }\n"
            "if (!ok) { adm.createUser({user: v.u, pwd: v.p,"
            " roles: [{role: 'userAdminAnyDatabase', db: 'admin'}]}); adm.auth(v.u, v.p); }\n"
            "const app = db.getSiblingDB(v.d);\n"
            "const roles = [{role: 'readWrite', db: v.d}];\n"
            "if (app.getUser(v.a)) { app.updateUser(v.a, {pwd: v.q, roles: roles}); } "
            "else { app.createUser({user: v.a, pwd: v.q, roles: roles}); }\n"
            f"print('{MARKER}');\n"
        )
        result = self._must(self._mongosh(script), "MongoDB user bootstrap")
        if MARKER.encode() not in result.stdout:
            raise SetupError("MongoDB user bootstrap did not confirm; receipt kept for replay")

    def _mongodb_verify(self) -> None:
        _admin, application = self._mongo_credentials()
        m, t = self.policy.mongodb, self.targets
        values = json.dumps({"a": m.application_user, "q": application, "d": t.archive_database})
        script = (
            f"const v = {values};\nconst app = db.getSiblingDB(v.d);\n"
            f"if (app.auth(v.a, v.q) && app.runCommand({{ping: 1}}).ok) print('{MARKER}');\n"
        )
        result = self._must(self._mongosh(script), "MongoDB login check")
        if MARKER.encode() not in result.stdout:
            raise SetupError("MongoDB application login failed with the stored credential")

    # Neo4j ----------------------------------------------------------------------------------

    def _neo4j_secrets(self) -> None:
        self._neo4j_password()

    def _neo4j_password(self) -> str:
        t, n = self.targets, self.policy.neo4j
        self._secret(t.graph_entry, t.graph_user_field, n.default_user)
        return self._secret(t.graph_entry, t.graph_password_field)

    def _neo4j_repository(self) -> None:
        self._repository(self.policy.neo4j)

    def _neo4j_config(self) -> None:
        """Neo4j ships its own file: the package goes in first with starts held, then the
        listener lines are replaced before the first start."""
        self._package(self.policy.neo4j)
        shipped = self._must(
            self._root(["cat", str(self.policy.neo4j.config_file)]), "read neo4j.conf"
        ).stdout.decode("utf-8")
        self._write(self.policy.neo4j.config_file, neo4j_config(shipped, self.targets).encode())

    def _neo4j_package(self) -> None:
        return

    def _neo4j_start(self) -> None:
        self._start(self.policy.neo4j, self.targets.neo4j_port)

    def _cypher(self, password: str, query: str, database: str = "system") -> Ran:
        n = self.policy.neo4j
        argv = [
            n.shell,
            "-a",
            f"bolt://127.0.0.1:{self.targets.neo4j_port}",
            "-d",
            database,
            "--format",
            "plain",
        ]
        env = {"NEO4J_USERNAME": n.default_user, "NEO4J_PASSWORD": password}
        return self._run(argv, query.encode(), env)

    def _neo4j_bootstrap(self) -> None:
        """The first login uses the default password and changes it at once; when the stored
        password already works the change happened before, and anything else is refused."""
        password = self._neo4j_password()
        if self._cypher(password, "RETURN 1;").code == 0:
            return
        if any(c in password for c in "'\\"):
            raise SetupError("generated Neo4j password has characters the change cannot carry")
        changed = self._cypher(
            self.policy.neo4j.default_password,
            f"ALTER CURRENT USER SET PASSWORD FROM '{self.policy.neo4j.default_password}' "
            f"TO '{password}';",
        )
        self._must(changed, "Neo4j initial password change")

    def _neo4j_verify(self) -> None:
        result = self._cypher(self._neo4j_password(), f"RETURN '{MARKER}';", "neo4j")
        if result.code or MARKER.encode() not in result.stdout:
            raise SetupError("Neo4j login failed with the stored credential")

    # Receipts -------------------------------------------------------------------------------

    def _load(self, name: str) -> Receipt | None:
        try:
            raw = self.files.read(self.state / f"{name}.json", self.policy.max_receipt_bytes)
        except FileNotFoundError:
            return None
        try:
            receipt = Receipt.model_validate_json(raw)
        except ValidationError as exc:
            raise SetupError(f"{name} install receipt is invalid") from exc
        if receipt.database != name:
            raise SetupError(f"{name} install receipt names another database")
        return receipt

    def _save(self, name: Literal["mongodb", "neo4j"], done: list[Step]) -> None:
        receipt = Receipt(version=1, database=name, inputs=self.inputs, done=tuple(done))
        self.files.save(self.state / f"{name}.json", receipt.model_dump_json().encode("utf-8"))
