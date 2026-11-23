"""Real, fixture-owned Neo4j container: no supplied URI or persistent host volume."""

import json
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager

from neo4j import GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError

IMAGE = (
    "neo4j:2026.10-community@"
    "sha256:4a2fd4f41ce384aa7d6a74b399f22c202e154f3966119c28a18ed9a500904f5e"
)


def docker(*arguments):
    executable = shutil.which("docker")
    if executable is None:
        raise AssertionError("Neo4j integration requires Docker; this gate cannot be skipped")
    return subprocess.run(  # noqa: S603 - discovered tool and fixture-owned arguments, no shell
        [executable, *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    ).stdout.strip()


@contextmanager
def disposable_graph():
    name = f"anamnema-test-{uuid.uuid4().hex}"
    container = docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--label",
        "anamnema.fixture=true",
        "--memory",
        "1536m",
        "--publish",
        "127.0.0.1::7687",
        "--env",
        "NEO4J_AUTH=none",
        "--env",
        "NEO4J_server_memory_heap_initial__size=256m",
        "--env",
        "NEO4J_server_memory_heap_max__size=512m",
        "--env",
        "NEO4J_server_memory_pagecache_size=128m",
        IMAGE,
    )
    driver = None
    volumes = []
    try:
        metadata = json.loads(docker("inspect", container))[0]
        assert metadata["Config"]["Labels"]["anamnema.fixture"] == "true"
        assert not [m for m in metadata["Mounts"] if m["Type"] == "bind"]
        volumes = [m["Name"] for m in metadata["Mounts"] if m["Type"] == "volume"]
        port = metadata["NetworkSettings"]["Ports"]["7687/tcp"][0]
        assert port["HostIp"] == "127.0.0.1"
        uri = f"bolt://127.0.0.1:{port['HostPort']}"
        driver = GraphDatabase.driver(uri, connection_timeout=1, max_transaction_retry_time=0)
        deadline = time.monotonic() + 90
        while True:
            try:
                driver.verify_connectivity()
                break
            except DriverError, Neo4jError, OSError:
                if time.monotonic() > deadline:
                    raise AssertionError(docker("logs", container)) from None
                time.sleep(0.2)
        yield driver, uri
    finally:
        if driver is not None:
            driver.close()
        docker("rm", "--force", "--volumes", container)
        for volume in volumes:
            assert not docker("volume", "ls", "--quiet", "--filter", f"name=^{volume}$")
