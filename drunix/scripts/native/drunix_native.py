#!/usr/bin/env python3
"""Run the Drunix test network as native processes (no Docker).

This is a faithful, container-free rendering of Drunix's own
``drunix-network/test-network/compose/compose-test-net.yaml``: every node is
started with the environment Drunix defines for it in that file. Only
host-specific values are rewritten:

  * container mount points      -> paths under the runtime directory
  * yugabyte-orgN:5433 (YSQL)   -> a local PostgreSQL (YSQL is PostgreSQL-wire
                                   compatible; Drunix talks to it through
                                   gorm's postgres driver)
  * hlf_keydb_orgNmsp:6379      -> local redis-server on the compose host ports
  * Docker chaincode launching  -> Fabric's chaincode-as-a-service builder
                                   (Drunix's ccaas_builder)

It exists for environments where the published Drunix images cannot run
(for example no Docker Hub access, or no amd64 emulation). On a laptop with
Docker the canonical path is Drunix's ./network.sh (see drunix/scripts/up.sh).

Commands: up | down [--purge] | status
"""
import argparse
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install -r drunix/scripts/native/requirements.txt")

HERE = Path(__file__).resolve().parent
AGENTGUARD_DRUNIX = HERE.parent.parent
RUNTIME = Path(os.environ.get("DRUNIX_RUNTIME_DIR", AGENTGUARD_DRUNIX / ".runtime"))
DRUNIX_HOME = Path(os.environ.get("DRUNIX_HOME", "")).expanduser()
PG_PORT = int(os.environ.get("DRUNIX_STATE_PG_PORT", "5433"))
PG_USER = "drunix"
PG_PASSWORD = os.environ.get("DRUNIX_STATE_PG_PASSWORD", "drunix-local-dev")
REDIS_PORTS = {"org1": 6479, "org2": 6389}  # host ports from scripts/yugabyte/compose.yaml
INFRA = os.environ.get("DRUNIX_INFRA", "local")  # local | docker
DOCKER_STATE = "agentguard-drunix-state"
DOCKER_KV = {"org1": "agentguard-drunix-kv-org1", "org2": "agentguard-drunix-kv-org2"}
OPS_PORTS = {"peer2.org1.example.com": 9464, "peer2.org2.example.com": 9465}
HOSTNAMES = ["orderer.example.com"] + [f"peer{p}.org{o}.example.com" for o in (1, 2) for p in (0, 1, 2)]
START_ORDER = ["orderer.example.com",
               "peer2.org1.example.com", "peer2.org2.example.com",   # validation services
               "peer1.org1.example.com", "peer1.org2.example.com",   # committing peers
               "peer0.org1.example.com", "peer0.org2.example.com"]   # lite peers


def log(msg):
    print(f"[drunix-native] {msg}", flush=True)


def die(msg):
    print(f"[drunix-native] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def port_open(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def wait_port(port, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return
        time.sleep(0.5)
    die(f"{what} did not open port {port} within {timeout}s (see {RUNTIME}/logs)")


def bin_dir():
    d = Path(os.environ.get("DRUNIX_BIN", DRUNIX_HOME / "build" / "bin"))
    for b in ("peer", "orderer", "vscc", "cryptogen", "configtxgen", "configtxlator", "osnadmin"):
        if not (d / b).exists():
            die(f"{d / b} not found. Build Drunix first: drunix/scripts/native/build.sh")
    return d


def ccaas_dir():
    d = Path(os.environ.get("DRUNIX_CCAAS_BUILDER", DRUNIX_HOME / "build" / "ccaas"))
    if not (d / "bin" / "detect").exists():
        die(f"{d}/bin/detect not found. Build Drunix first: drunix/scripts/native/build.sh")
    return d


def pg_bin():
    explicit = os.environ.get("DRUNIX_PG_BIN")
    cands = [Path(explicit)] if explicit else []
    try:
        cands.append(Path(subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True).stdout.strip()))
    except FileNotFoundError:
        pass
    cands += sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True)
    cands += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    for c in cands:
        if (c / "initdb").exists() and (c / "pg_ctl").exists():
            return c
    die("PostgreSQL server binaries (initdb, pg_ctl) not found; set DRUNIX_PG_BIN")


def as_pg_user(cmd):
    """initdb/postgres refuse to run as root; drop to the 'postgres' account."""
    if os.geteuid() != 0:
        return cmd
    return ["runuser", "-u", "postgres", "--"] + cmd


def check_hosts():
    missing = []
    for h in HOSTNAMES:
        try:
            if socket.gethostbyname(h) not in ("127.0.0.1",):
                missing.append(h)
        except socket.gaierror:
            missing.append(h)
    if missing:
        die("these node hostnames must resolve to 127.0.0.1 (add to /etc/hosts): " + " ".join(missing) +
            "\n  sudo sh -c 'echo \"127.0.0.1 " + " ".join(HOSTNAMES) + "\" >> /etc/hosts'")


# ── runtime workspace ────────────────────────────────────────────────────────

def prepare_workspace(bins):
    """Copy the parts of Drunix's test-network needed at runtime (never the
    committed sample crypto) and generate fresh crypto with cryptogen."""
    src = DRUNIX_HOME / "drunix-network"
    tn = RUNTIME / "test-network"
    for sub in ("scripts", "configtx", "compose"):
        dst = tn / sub
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src / "test-network" / sub, dst)
    for f in ("network.config", "setOrgEnv.sh"):
        shutil.copy2(src / "test-network" / f, tn / f)
    (tn / "organizations").mkdir(parents=True, exist_ok=True)
    for sub in ("cryptogen",):
        dst = tn / "organizations" / sub
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src / "test-network" / "organizations" / sub, dst)
    for tpl in (src / "test-network" / "organizations").glob("ccp-template.*"):
        shutil.copy2(tpl, tn / "organizations")
    shutil.copy2(src / "test-network" / "organizations" / "ccp-generate.sh", tn / "organizations")
    cfg = RUNTIME / "config"
    if cfg.exists():
        shutil.rmtree(cfg)
    shutil.copytree(src / "config", cfg)

    # Peer configuration: Drunix's peercfg/core.yaml with the ccaas builder path
    # pointed at the locally built Drunix ccaas_builder.
    peercfg = RUNTIME / "peercfg"
    if peercfg.exists():
        shutil.rmtree(peercfg)
    shutil.copytree(tn / "compose" / "docker" / "peercfg", peercfg)
    core = (peercfg / "core.yaml").read_text()
    core = core.replace("/opt/hyperledger/ccaas_builder", str(ccaas_dir()))
    (peercfg / "core.yaml").write_text(core)

    if not (tn / "organizations" / "peerOrganizations").exists():
        log("generating crypto material with Drunix cryptogen")
        for f in ("crypto-config-org1.yaml", "crypto-config-org2.yaml", "crypto-config-orderer.yaml"):
            subprocess.run([str(bins / "cryptogen"), "generate", f"--config=./organizations/cryptogen/{f}",
                            "--output=organizations"], cwd=tn, check=True, stdout=subprocess.DEVNULL)
        env = dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}")
        subprocess.run(["bash", "./organizations/ccp-generate.sh"], cwd=tn, check=True, env=env,
                       stdout=subprocess.DEVNULL)
    (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
    (RUNTIME / "pids").mkdir(parents=True, exist_ok=True)


# ── state database + transient store ────────────────────────────────────────

def start_postgres(pgb):
    data = RUNTIME / "pgdata"
    sock = RUNTIME / "pgsock"
    sock.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        pw = pwd.getpwnam("postgres")
        os.chown(sock, pw.pw_uid, pw.pw_gid)
    if not (data / "PG_VERSION").exists():
        data.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            pw = pwd.getpwnam("postgres")
            os.chown(data, pw.pw_uid, pw.pw_gid)
        pwfile = RUNTIME / ".pgpw"
        pwfile.write_text(PG_PASSWORD + "\n")
        pwfile.chmod(0o644)
        log("initialising the Drunix state database (PostgreSQL, stands in for YugabyteDB YSQL)")
        subprocess.run(as_pg_user([str(pgb / "initdb"), "-D", str(data), "-U", PG_USER, "--auth=scram-sha-256",
                                   f"--pwfile={pwfile}", "-E", "UTF8"]), check=True, stdout=subprocess.DEVNULL)
        pwfile.unlink()
    if port_open(PG_PORT):
        log(f"state database already listening on {PG_PORT}")
    else:
        subprocess.run(as_pg_user([str(pgb / "pg_ctl"), "-D", str(data), "-l", str(data / "postgres.log"),
                                   "-o", f"-p {PG_PORT} -k {sock} -c listen_addresses=127.0.0.1 -c max_connections=400",
                                   "-w", "start"]), check=True, stdout=subprocess.DEVNULL)
    env = dict(os.environ, PGPASSWORD=PG_PASSWORD)
    for db in ("drunix_org1", "drunix_org2"):
        r = subprocess.run([str(pgb / "psql"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", PG_USER, "-d", "postgres",
                            "-tAc", f"SELECT 1 FROM pg_database WHERE datname='{db}'"], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            die("cannot connect to the state database: " + r.stderr.strip())
        if r.stdout.strip() != "1":
            subprocess.run([str(pgb / "createdb"), "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", PG_USER, db],
                           check=True, env=env)
            log(f"created state database {db}")


def _docker(*args, check=True, capture=False):
    return subprocess.run(["docker", *args], check=check, text=True,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE if capture else None)


def _container_state(name):
    r = _docker("inspect", "-f", "{{.State.Running}}", name, check=False, capture=True)
    return None if r.returncode != 0 else r.stdout.strip() == "true"


def _ensure_container(name, run_args):
    """Start (or create) one of OUR containers. Only names prefixed
    agentguard-drunix- are ever created, started or stopped."""
    assert name.startswith("agentguard-drunix-")
    state = _container_state(name)
    if state is not None:
        # Containers created before the restart policy was added: give them one,
        # so a Docker Desktop restart / Mac reboot brings the state DB back.
        _docker("update", "--restart", "unless-stopped", name, check=False)
    if state is True:
        log(f"container {name} already running")
    elif state is False:
        _docker("start", name)
        log(f"started existing container {name}")
    else:
        _docker("run", "-d", "--name", name, "--label", "agentguard.drunix=1",
                "--restart", "unless-stopped", *run_args)
        log(f"created container {name}")


def start_infra_docker():
    """State DB + transient store as containers (multi-arch official images,
    native on Apple Silicon). Host ports are loopback-only."""
    if not shutil.which("docker"):
        die("DRUNIX_INFRA=docker needs the docker CLI")
    if _container_state(DOCKER_STATE) is not True and port_open(PG_PORT):
        # Port taken while our container is not running. If it is the local
        # cluster this script creates for DRUNIX_INFRA=local (runtime/pgdata),
        # stop it; its data directory is kept. Anything else is not ours.
        if (RUNTIME / "pgdata" / "postmaster.pid").exists():
            log(f"stopping the local state database on {PG_PORT} (DRUNIX_INFRA=local leftover; data kept in {RUNTIME / 'pgdata'})")
            subprocess.run(as_pg_user([str(pg_bin() / "pg_ctl"), "-D", str(RUNTIME / "pgdata"), "-m", "fast", "-w", "stop"]),
                           stdout=subprocess.DEVNULL)
        if port_open(PG_PORT):
            die(f"port {PG_PORT} is in use by another process; {DOCKER_STATE} needs it (lsof -nP -iTCP:{PG_PORT} -sTCP:LISTEN)")
    _ensure_container(DOCKER_STATE, ["-p", f"127.0.0.1:{PG_PORT}:5432", "-e", f"POSTGRES_USER={PG_USER}",
                                     "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}", "postgres:16-alpine",
                                     "-c", "max_connections=400"])
    for org, port in REDIS_PORTS.items():
        _ensure_container(DOCKER_KV[org], ["-p", f"127.0.0.1:{port}:6379", "redis:7-alpine",
                                           "redis-server", "--save", "", "--appendonly", "no"])
    for _ in range(60):
        if _docker("exec", DOCKER_STATE, "pg_isready", "-U", PG_USER, check=False).returncode == 0:
            break
        time.sleep(1)
    else:
        die("state database container did not become ready")
    for db in ("drunix_org1", "drunix_org2"):
        r = _docker("exec", DOCKER_STATE, "psql", "-U", PG_USER, "-d", "postgres", "-tAc",
                    f"SELECT 1 FROM pg_database WHERE datname='{db}'", capture=True)
        if r.stdout.strip() != "1":
            _docker("exec", DOCKER_STATE, "createdb", "-U", PG_USER, db)
            log(f"created state database {db}")
    for port in list(REDIS_PORTS.values()) + [PG_PORT]:
        wait_port(port, 30, f"infra port {port}")


def redis_exe():
    exe = shutil.which("redis-server") or shutil.which("keydb-server")
    if not exe:
        die("redis-server (or keydb-server) not found; it stands in for Drunix's KeyDB transient store. "
            "Install it (brew install redis) or set DRUNIX_INFRA=docker in drunix/drunix.env")
    return exe


def start_redis():
    exe = redis_exe()
    for org, port in REDIS_PORTS.items():
        if port_open(port):
            log(f"KeyDB/redis for {org} already listening on {port}")
            continue
        d = RUNTIME / "kvstore" / org
        d.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen([exe, "--port", str(port), "--bind", "127.0.0.1", "--dir", str(d), "--save", "",
                              "--appendonly", "no", "--protected-mode", "yes"],
                             stdout=open(RUNTIME / "logs" / f"kvstore-{org}.log", "w"), stderr=subprocess.STDOUT,
                             start_new_session=True)
        (RUNTIME / "pids" / f"kvstore-{org}.pid").write_text(str(p.pid))
        wait_port(port, 15, f"kvstore {org}")


# ── Drunix nodes ─────────────────────────────────────────────────────────────

def load_services():
    compose_dir = RUNTIME / "test-network" / "compose"
    base = yaml.safe_load((compose_dir / "compose-test-net.yaml").read_text())["services"]
    over = yaml.safe_load((compose_dir / "docker" / "docker-compose-test-net.yaml").read_text())["services"]
    services = {}
    for name, svc in base.items():
        env = {}
        for item in (svc.get("environment") or []) + ((over.get(name) or {}).get("environment") or []):
            k, _, v = item.partition("=")
            env[k.strip()] = v.strip().strip('"')
        vols = list(svc.get("volumes") or []) + list((over.get(name) or {}).get("volumes") or [])
        services[name] = {"env": env, "volumes": vols, "command": svc["command"], "ports": svc.get("ports") or []}
    return services


def node_environment(name, svc, bins):
    compose_dir = RUNTIME / "test-network" / "compose"
    node_dir = RUNTIME / "nodes" / name
    node_dir.mkdir(parents=True, exist_ok=True)
    mounts = []
    for v in svc["volumes"]:
        host, _, container = str(v).partition(":")
        container = container.split(":")[0]
        if "docker.sock" in host or "DOCKER_SOCK" in host:
            continue
        if host.startswith(".") or host.startswith("/"):
            hp = (compose_dir / host).resolve() if host.startswith(".") else Path(host)
        elif container.endswith("peercfg"):
            continue
        else:  # named volume
            hp = node_dir / "production"
            hp.mkdir(parents=True, exist_ok=True)
        mounts.append((container.rstrip("/"), str(hp)))
    mounts.append(("/tmp/drunix-sharding", str(node_dir / "sharding")))
    mounts.sort(key=lambda m: len(m[0]), reverse=True)

    env = {}
    for k, v in svc["env"].items():
        # Rewrite container paths wherever they appear (plain values and lists
        # such as "[/var/hyperledger/orderer/tls/ca.crt]").
        placeholders = {}
        for i, (cpath, hpath) in enumerate(mounts):
            token = f"\x00{i}\x00"
            v2 = re.sub(re.escape(cpath) + r"(?=/|$|\]|,)", token, v)
            if v2 != v:
                placeholders[token] = hpath
                v = v2
        for token, hpath in placeholders.items():
            v = v.replace(token, hpath)
        env[k] = v

    is_orderer = svc["command"].startswith("orderer")
    org = "org2" if ".org2." in name else "org1"
    if is_orderer:
        env["FABRIC_CFG_PATH"] = str(RUNTIME / "config")
        env.setdefault("ORDERER_OPERATIONS_LISTENADDRESS", "127.0.0.1:9443")
        (node_dir / "sharding").mkdir(parents=True, exist_ok=True)
        env["ORDERER_FILELEDGER_LOCATION"] = str(node_dir / "production" / "ledger")
        env["ORDERER_FILELEDGER_KVSTORE"] = str(node_dir / "production" / "kvstore")
        env["ORDERER_CONSENSUS_WALDIR"] = str(node_dir / "production" / "etcdraft" / "wal")
        env["ORDERER_CONSENSUS_SNAPDIR"] = str(node_dir / "production" / "etcdraft" / "snapshot")
    else:
        env["FABRIC_CFG_PATH"] = str(RUNTIME / "peercfg")
        env["CORE_PEER_FILESYSTEMPATH"] = str(node_dir / "production")
        # core.yaml defaults ledger.snapshots.rootDir to /var/hyperledger/...,
        # which only a root user can create (fails on macOS / non-root Linux).
        env["CORE_LEDGER_SNAPSHOTS_ROOTDIR"] = str(node_dir / "production" / "snapshots")
        (node_dir / "production").mkdir(parents=True, exist_ok=True)
        env["CORE_VM_ENDPOINT"] = ""  # chaincode runs as a service, not in Docker
        if "CORE_LEDGER_STATE_SQLDBCONFIG_ADDRESS" in env:
            env["CORE_LEDGER_STATE_SQLDBCONFIG_ADDRESS"] = "127.0.0.1"
            env["CORE_LEDGER_STATE_SQLDBCONFIG_PORT"] = str(PG_PORT)
            env["CORE_LEDGER_STATE_SQLDBCONFIG_DBNAME"] = f"drunix_{org}"
            env["CORE_LEDGER_STATE_SQLDBCONFIG_USER"] = PG_USER
            env["CORE_LEDGER_STATE_SQLDBCONFIG_PASSWORD"] = PG_PASSWORD
        for k in ("CORE_PEER_KVSTORE_ADDRESS", "CORE_PEER_KVSTORE_REPLICAADDRESS"):
            if k in env:
                env[k] = f"127.0.0.1:{REDIS_PORTS[org]}"
        if name in OPS_PORTS:
            env["CORE_OPERATIONS_LISTENADDRESS"] = f"127.0.0.1:{OPS_PORTS[name]}"
    # Nodes talk to each other directly: never inherit a host HTTP(S) proxy
    # (gRPC-Go honours HTTPS_PROXY for every dial).
    dropped = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("CORE_") or k.startswith("ORDERER_") or k == "FABRIC_CFG_PATH" or k in dropped)}
    base["PATH"] = f"{bins}:{base.get('PATH', '')}"
    base.update(env)
    return base


def listen_port(name, svc):
    ports = svc["ports"]
    return int(str(ports[0]).split(":")[0]) if ports else None


def start_nodes(bins):
    services = load_services()
    missing = [n for n in START_ORDER if n not in services]
    if missing:
        die(f"compose file no longer defines {missing}; update START_ORDER")
    for name in START_ORDER:
        svc = services[name]
        port = listen_port(name, svc)
        pidf = RUNTIME / "pids" / f"{name}.pid"
        if port and port_open(port):
            log(f"{name} already listening on {port}")
            continue
        env = node_environment(name, svc, bins)
        (RUNTIME / "nodes" / name / "env.json").write_text(json.dumps(
            {k: ("***" if "PASSWORD" in k else v) for k, v in env.items() if k.startswith(("CORE_", "ORDERER_", "FABRIC_"))},
            indent=1, sort_keys=True))
        cmd = svc["command"].split()
        cmd[0] = str(bins / cmd[0])
        p = subprocess.Popen(cmd, env=env, cwd=RUNTIME / "nodes" / name,
                             stdout=open(RUNTIME / "logs" / f"{name}.log", "w"), stderr=subprocess.STDOUT,
                             start_new_session=True)
        pidf.write_text(str(p.pid))
        log(f"started {name} ({svc['command']}) pid {p.pid}")
        if port:
            wait_port(port, 60, name)


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_up(_):
    if not DRUNIX_HOME or not (DRUNIX_HOME / "drunix-network").exists():
        die("set DRUNIX_HOME to the Drunix repository")
    check_hosts()
    bins = bin_dir()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    prepare_workspace(bins)
    if INFRA == "docker":
        start_infra_docker()
    else:
        pgb = pg_bin()
        redis_exe()  # check both prerequisites before creating or starting anything
        start_postgres(pgb)
        start_redis()
    start_nodes(bins)
    log("Drunix network is up (orderer, 2 x lite peer, 2 x committing peer, 2 x validation service)")


def _pid_command(pid):
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _stop_pid(pidfile, expect=None):
    try:
        pid = int(pidfile.read_text())
    except (ValueError, FileNotFoundError):
        return
    if expect:
        # A node that crashed leaves its pidfile behind, and PIDs are recycled:
        # only signal the process if it is still the one we started.
        cmdline = _pid_command(pid)
        if not any(e in cmdline for e in expect):
            if cmdline:
                log(f"{pidfile.stem}: pid {pid} is now another process ({cmdline[:60]}); not stopping it")
            pidfile.unlink(missing_ok=True)
            return
    def signal_it(sig):
        # Nodes run in their own process group (start_new_session); processes
        # started by the shell helpers (chaincode servers) may not.
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    signal_it(signal.SIGTERM)
    for _ in range(50):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    else:
        signal_it(signal.SIGKILL)
    pidfile.unlink(missing_ok=True)


def cmd_down(args):
    pids = RUNTIME / "pids"
    if pids.exists():
        for f in sorted(pids.glob("chaincode-*.pid")) + [pids / f"{n}.pid" for n in reversed(START_ORDER)] + \
                sorted(pids.glob("kvstore-*.pid")):
            if f.exists():
                log(f"stopping {f.stem}")
                stem = f.stem
                expect = ([str(RUNTIME / "chaincode")] if stem.startswith("chaincode-") else
                          ["redis", "keydb"] if stem.startswith("kvstore-") else
                          ["/orderer"] if stem.startswith("orderer") else ["/peer node", "/vscc"])
                _stop_pid(f, expect)
    if INFRA == "docker":
        for name in [DOCKER_STATE, *DOCKER_KV.values()]:
            if _container_state(name) is not None:
                log(f"stopping container {name}")
                _docker("rm" if args.purge else "stop", *(["-f"] if args.purge else []), name, check=False)
    data = RUNTIME / "pgdata"
    if (data / "postmaster.pid").exists():
        log("stopping state database")
        subprocess.run(as_pg_user([str(pg_bin() / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"]),
                       stdout=subprocess.DEVNULL)
    if args.purge:
        log(f"removing {RUNTIME}")
        shutil.rmtree(RUNTIME, ignore_errors=True)


def _pid_alive(pidfile):
    """Portable liveness check (Linux and macOS; no /proc needed)."""
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
        return True
    except PermissionError:  # exists, owned by another user
        return True
    except (ValueError, OSError):
        return False


def cmd_status(_):
    rows = [("state-db (postgres)", PG_PORT)] + [(f"kvstore {o}", p) for o, p in REDIS_PORTS.items()]
    if (RUNTIME / "test-network" / "compose").exists():
        services = load_services()
        rows += [(n, listen_port(n, services[n])) for n in START_ORDER]
    ok = True
    for name, port in rows:
        up = port_open(port) if port else False
        ok &= up
        print(f"  {'UP  ' if up else 'DOWN'} {name:28s} :{port}")
    for f in sorted((RUNTIME / "pids").glob("chaincode-*.pid")) if (RUNTIME / "pids").exists() else []:
        print(f"  {'UP  ' if _pid_alive(f) else 'DOWN'} {f.stem}")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    d = sub.add_parser("down")
    d.add_argument("--purge", action="store_true", help="also delete ledgers, state database and crypto")
    sub.add_parser("status")
    args = ap.parse_args()
    {"up": cmd_up, "down": cmd_down, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
