"""Build Review in a disposable process and hand a saved generation to the web.

The helper coordinates the build and restart. Snapshot construction and loading
run as the unprivileged web user, which also owns local snapshots.
"""

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from bto.log_errors import log_failure
from bto.web.publication_host import active_jobs, active_threads
from bto.web.publication_host import load as host_configuration

log = logging.getLogger(__name__)
WORKING = {"pending", "building", "waiting_for_jobs", "switching"}


def directory():
    state = os.environ.get("STATE_DIRECTORY")
    if state:
        return Path(state.split(":")[0])
    explicit = os.environ.get("BTO_REVIEW_SNAPSHOT_FILE")
    if explicit:
        return Path(explicit).resolve().parent
    raise ValueError("A private Review state directory is required")


def read_json(path):
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    """Atomic private metadata, including when the coordinator writes for the web."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o600)
            if os.geteuid() == 0:
                owner = path.parent.stat()
                os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            handle.write(json.dumps(value, default=str).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def request_publication(request_id=None, source_published_at=None):
    """Save one publication request unless a worker already owns one."""
    root = directory()
    identifier = request_id or uuid4().hex
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise ValueError("Invalid publication request identifier")
    with (root / "review_publication.request.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        receipt = read_json(root / "review_publication.json")
        if (root / "review_publication.busy").exists() or (root / "review_publication.request.json").exists() or (
            receipt.get("status") in WORKING and time.time() - receipt.get("updated_at", 0) < 3600
        ):
            return False
        request = {"request_id": identifier, "source_published_at": source_published_at,
                   "status": "pending", "updated_at": time.time()}
        write_json(root / "review_publication.json", request)
        # The nightly caller consumes this authenticated request before building.
        write_json(root / "review_publication.request.json", request)
    return identifier


def publication_status(snapshot):
    """Confirm a completed receipt only when its snapshot is loaded."""
    receipt = read_json(directory() / "review_publication.json")
    status, identifier = receipt.get("status"), receipt.get("request_id")
    confirmed = bool(snapshot and receipt.get("built_at") == snapshot.built_at.isoformat())
    return {
        "building": status in WORKING or (directory() / "review_publication.busy").exists(),
        "request_id": identifier,
        "completed_request": identifier if status == "complete" and confirmed else None,
        "failed_request": identifier if status == "failed" else None,
        "error": receipt.get("error"),
        "publication_phase": status,
        "process_memory": _memory(),
    }


def _memory():
    from bto.web.review_cache_memory import memory_usage

    return memory_usage()


def install_handover(app):
    """Stop admitting work before checking that requests and paid jobs have drained."""
    if os.environ.get("BTO_REVIEW_EXTERNAL_PUBLICATION") != "1":
        return
    from flask import g, request

    host = host_configuration(privileged=True)
    root, lock = directory(), threading.Lock()
    state = {"requests": 0}

    @app.before_request
    def admit():
        control = request.path in {"/review/cache/status", "/login"}
        with lock:
            stage = read_json(root / "review_handover.drain").get("stage")
            if stage and not control and (stage == "all" or request.method not in {"GET", "HEAD", "OPTIONS"}):
                return ("Review is publishing an update. Please try again shortly.", 503,
                        {"Retry-After": "5"})
            if not control:
                state["requests"] += 1
                g.handover_counted = True

    @app.teardown_request
    def finished(error):
        if getattr(g, "handover_counted", False):
            with lock:
                state["requests"] -= 1
                g.handover_counted = False

    def monitor():
        last_write, last_snapshot = 0, None
        while True:
            try:
                from bto.web.routes.review import hot

                snapshot = hot.snapshot
                built_at = snapshot.built_at.isoformat() if snapshot else None
                draining = (root / "review_handover.drain").exists()
                if draining or built_at != last_snapshot or time.time() - last_write >= 30:
                    jobs = None
                    if draining:
                        # These existing tables own the actual work; no provider is called.
                        with app.config["PG_CONNECT"]() as conn, conn.transaction():
                            conn.execute("SET TRANSACTION READ ONLY")
                            conn.execute("SET LOCAL statement_timeout = '5s'")
                            jobs = active_jobs(conn, host)
                        # Configured host workers can hold queued work between database runs.
                        jobs += active_threads(host)
                    with lock:
                        active = state["requests"]
                        gate = read_json(root / "review_handover.drain")
                    write_json(root / "review_handover.json", {
                        "pid": os.getpid(), "updated_at": time.time(), "requests": active,
                        "jobs": jobs, "draining": draining, "built_at": built_at,
                        "drain_stage": gate.get("stage"), "drain_request": gate.get("request_id"),
                        "format": getattr(snapshot, "format", None),
                        "retained_bytes": snapshot.retained_bytes if snapshot else None,
                        "process_memory": _memory(),
                    })
                    last_write, last_snapshot = time.time(), built_at
                    del snapshot
            except Exception as error:
                log_failure(log, "Review handover not ready", error=error, level=logging.WARNING)
            time.sleep(1)

    # Wait until app startup has attached the HotCache, without touching providers.
    threading.Thread(target=monitor, daemon=True, name="review-handover").start()


def build_candidate():
    """Build and save the requested candidate as the unprivileged web user."""
    if os.geteuid() == 0:
        raise RuntimeError("Snapshot building must run as the unprivileged web user")
    from bto import settings
    from bto.web import review_cache

    settings.load_postgres()
    root = directory()
    request = read_json(root / "review_publication.json")
    snapshot = review_cache.build(source_published_at=request.get("source_published_at"))
    os.environ["BTO_REVIEW_SNAPSHOT_FILE"] = str(root / "review_snapshot.next.pickle")
    review_cache.save(snapshot)
    write_json(root / "review_snapshot.next.json", {
        "request_id": request["request_id"], "built_at": snapshot.built_at.isoformat(),
        "groups": len(snapshot.rows), "retained_bytes": snapshot.retained_bytes,
        "coverage_since": snapshot.coverage_since, "coverage_until": snapshot.coverage_until,
        "builder_memory": _memory(), "build_seconds": snapshot.build_seconds,
    })
    log.info("Review candidate saved groups=%s bytes=%s build_seconds=%s memory=%s",
             len(snapshot.rows), snapshot.retained_bytes, snapshot.build_seconds, _memory())


def wait_until(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError(message)


def _web_pid():
    unit = host_configuration(privileged=True)["service"]
    return int(subprocess.check_output(["systemctl", "show", unit, "-p", "MainPID", "--value"], text=True).strip())


def _ready(root, pid, *, idle=False, built_at=None, stage=None, request_id=None):
    value = read_json(root / "review_handover.json")
    if value.get("pid") != pid or time.time() - value.get("updated_at", 0) > 5:
        return False
    if idle and not (value.get("draining") and value.get("jobs") == 0 and value.get("requests") == 0):
        return False
    if stage and (value.get("drain_stage") != stage or value.get("drain_request") != request_id):
        return False
    if built_at and value.get("built_at") != built_at:
        return False
    return value


def _restart():
    unit = host_configuration(privileged=True)["service"]
    subprocess.run(["systemctl", "restart", unit], check=True, timeout=60)


def recover(root=None):
    """A failed switch restores the previous disk generation without reading its pickle."""
    root = root or directory()
    receipt = read_json(root / "review_publication.json")
    if receipt.get("status") == "complete":
        for name in ["review_handover.drain", "review_snapshot.previous.pickle", "review_snapshot.next.json", "review_publication.busy"]:
            (root / name).unlink(missing_ok=True)
        return
    previous = root / "review_snapshot.previous.pickle"
    if previous.exists():
        log.warning("Review publication restoring previous snapshot request_id=%s",
                    receipt.get("request_id"))
        os.replace(previous, root / "review_snapshot.pickle")
        _restart()
    receipt.update(status="failed", error=receipt.get("error") or "Review publication worker stopped; previous snapshot retained",
                   updated_at=time.time())
    write_json(root / "review_publication.json", receipt)
    for name in ["review_handover.drain", "review_snapshot.next.pickle", "review_snapshot.next.json", "review_publication.busy"]:
        (root / name).unlink(missing_ok=True)
    log.warning("Review publication recovery complete request_id=%s; previous snapshot retained",
                receipt.get("request_id"))


def builder_environment(pid):
    """Use the serving role's database settings, never nightly/provider secrets."""
    fields = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    environment = {}
    for field in fields:
        key, separator, value = field.partition(b"=")
        if separator and key.startswith(b"PG"):
            environment[os.fsdecode(key)] = os.fsdecode(value)
    environment.update(PATH="/usr/bin:/bin", HOME=str(directory()),
                       STATE_DIRECTORY=str(directory()), PYTHONDONTWRITEBYTECODE="1")
    return environment


def recover_inactive(request_id):
    """A retry may recover a dead child, but must leave a live publication alone."""
    host = host_configuration(privileged=True)
    root = directory()
    if root.resolve() != Path(host["state_directory"]).resolve():
        raise ValueError("Review state directory differs from private host configuration")
    with os.fdopen(os.open(root / "review_publication.worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = read_json(root / "review_publication.json")
        if receipt.get("request_id") != request_id:
            raise ValueError("Review recovery identifier does not match its receipt")
        if receipt.get("status") == "complete":
            recover(root)
            return
        if receipt.get("status") == "pending" and (root / "review_publication.request.json").exists():
            return
        if receipt.get("status") in WORKING or (root / "review_snapshot.previous.pickle").exists():
            # The coordinator is gone; stop any builder left in its bounded scope.
            subprocess.run(["systemctl", "stop", "bto-review-publication-" + request_id + ".scope"],
                           check=False, timeout=120)
            recover(root)


def coordinate(request_id=None):
    """Build, drain requests, switch snapshots and verify the restarted web process."""
    if os.geteuid() != 0:
        raise RuntimeError("Only the nightly publication helper coordinates restart")
    host = host_configuration(privileged=True)
    root = directory()
    if root.resolve() != Path(host["state_directory"]).resolve():
        raise ValueError("Review state directory differs from private host configuration")
    with os.fdopen(os.open(root / "review_publication.worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request_path = root / "review_publication.request.json"
        request = read_json(request_path)
        if not request:
            receipt = read_json(root / "review_publication.json")
            if request_id and receipt.get("request_id") != request_id:
                raise ValueError("Review publication identifier does not match its receipt")
            return
        identifier = request.get("request_id", "")
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("Invalid publication identifier")
        if request_id and identifier != request_id:
            raise ValueError("Review publication identifier does not match its request")
        request_path.unlink()
        receipt = {**request, "status": "building", "updated_at": time.time()}
        write_json(root / "review_publication.busy", {"request_id": identifier})

        def update(**values):
            previous_status = receipt.get("status")
            receipt.update(values, updated_at=time.time())
            write_json(root / "review_publication.json", receipt)
            if receipt["status"] != previous_status:
                log.info("Review publication phase request_id=%s phase=%s memory=%s",
                         identifier, receipt["status"], _memory())

        try:
            update(previous_snapshot=read_json(root / "review_handover.json").get("built_at"))
            update(status="waiting_for_jobs")
            write_json(root / "review_handover.drain", {"stage": "jobs", "request_id": identifier})
            pid = _web_pid()
            wait_until(lambda: _ready(root, pid, idle=True, stage="jobs", request_id=identifier),
                       1800, "Review publication deferred: active web work has not finished")
            # The builder receives the web role's existing environment. It cannot spend.
            def has_headroom():
                # Read one coherent process/host measurement per check.
                usage = _memory()
                return not usage or usage[1] >= 256 * 1024 * 1024

            wait_until(has_headroom,
                       600, "Review publication deferred: insufficient host headroom")
            update(status="building")
            subprocess.run([host["python"], "-m", "bto.web.build_review_snapshot", "--build"],
                           user=host["user"], group=host["group"], extra_groups=[],
                           env=builder_environment(pid), cwd=host["working_directory"],
                           check=True, timeout=1000)
            candidate = read_json(root / "review_snapshot.next.json")
            if candidate.get("request_id") != identifier or not candidate.get("built_at"):
                raise RuntimeError("Review builder did not save the requested candidate")
            # The child has exited before the web can load the candidate.
            update(**candidate)
            write_json(root / "review_handover.drain", {"stage": "all", "request_id": identifier})
            time.sleep(2)
            wait_until(lambda: _ready(root, pid, idle=True, stage="all", request_id=identifier), 120,
                       "Review publication deferred: active requests have not finished")
            update(status="switching")
            current, previous = root / "review_snapshot.pickle", root / "review_snapshot.previous.pickle"
            if previous.exists():
                raise RuntimeError("A previous Review handover still requires recovery")
            if current.exists():
                # A second name for the old generation preserves an atomic current-file swap.
                os.link(current, previous)
            os.replace(root / "review_snapshot.next.pickle", current)
            _restart()
            new_pid = _web_pid()
            ready = wait_until(lambda: _ready(root, new_pid, built_at=candidate["built_at"]),
                               120, "New Review snapshot did not become serving")
            # Verify an actual HTTP response as well as the exact loaded generation.
            import urllib.request

            with urllib.request.urlopen(host["health_url"], timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError("Restarted Review HTTP check failed")
            update(status="complete", web_pid=new_pid, web_memory=ready.get("process_memory"), error=None)
            previous.unlink(missing_ok=True)
            for name in ["review_handover.drain", "review_snapshot.next.json", "review_publication.busy"]:
                (root / name).unlink(missing_ok=True)
            log.info("Review publication confirmed request=%s built_at=%s web_pid=%s memory=%s",
                     identifier, candidate["built_at"], new_pid, ready.get("process_memory"))
        except Exception as error:
            log_failure(
                log, "Review publication failed request_id=%s phase=%s; preserving previous snapshot",
                identifier, receipt["status"], error=error,
            )
            update(error=str(error)[:500])
            recover(root)
            raise


def main():
    """Run the requested build, publication or recovery mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--publish", action="store_true")
    mode.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.build:
        build_candidate()
    else:
        if os.geteuid() != 0:
            raise RuntimeError("Publication and recovery belong to the nightly helper")
        if not args.request_id or len(args.request_id) != 32 or any(c not in "0123456789abcdef" for c in args.request_id):
            parser.error("publication and recovery require a valid --request-id")

        def interrupted(signum, frame):
            raise RuntimeError("Review publication interrupted")

        signal.signal(signal.SIGTERM, interrupted)
        try:
            if args.publish:
                coordinate(args.request_id)
            else:
                recover_inactive(args.request_id)
        except BlockingIOError:
            # Another child still owns this publication; the caller polls its receipt.
            raise SystemExit(75) from None


if __name__ == "__main__":
    main()
