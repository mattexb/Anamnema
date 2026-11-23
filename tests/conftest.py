"""Real disposable Mongo process; never reuse an operator-supplied database."""

import shutil
import socket
import subprocess
import time

import pytest
from extraction_fixtures import ModelService
from graph_fixtures import disposable_graph
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from anamnema.graph_store import GraphStore
from anamnema.memory_config import load_memory


@pytest.fixture(scope="session")
def graph():
    with disposable_graph() as (driver, _):
        store = GraphStore(driver, load_memory().model_copy(update={"dimensions": 4}))
        store.ensure_schema()
        yield store


@pytest.fixture
def model_service(tmp_path):
    model = tmp_path / "fixture.gguf"
    model.write_bytes(b"GGUF synthetic fixture, not a usable model")
    with ModelService(model) as service:
        yield service


@pytest.fixture(scope="session")
def disposable_mongo(tmp_path_factory: pytest.TempPathFactory):
    executable = shutil.which("mongod")
    if executable is None:
        pytest.fail("Mongo integration requires mongod; missing dependency is not a skipped gate")
    folder = tmp_path_factory.mktemp("anamnema-mongo")
    database_path = folder / "db"
    database_path.mkdir()
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    command = [
        executable,
        "--dbpath",
        str(database_path),
        "--bind_ip",
        "127.0.0.1",
        "--port",
        str(port),
        "--nounixsocket",
        "--noauth",
        "--wiredTigerCacheSizeGB",
        "0.25",
    ]
    uri = f"mongodb://127.0.0.1:{port}"
    client = MongoClient(uri, directConnection=True, serverSelectionTimeoutMS=200)
    with (folder / "process.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, stdout=output, stderr=output)  # noqa: S603 - discovered server and fixture-owned arguments
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(
                        f"disposable mongod failed to start; inspect {folder / 'process.log'}"
                    )
                try:
                    options = client.admin.command("getCmdLineOpts")
                    assert options["parsed"]["storage"]["dbPath"] == str(database_path)
                    break
                except PyMongoError:
                    time.sleep(0.1)
            yield uri, client
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
