"""Run collection, cleaning, Batch sorting and Review refresh to one terminal email."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from bto.filter_and_bucket_job_listings import night
from bto.filter_and_bucket_job_listings.call_gemini_batch import save, timestamp


def execute(state, path, commands, *, invoke=subprocess.run, check_collection=None):
    """Each command keeps its own retry policy. Do not restart paid collection."""
    stages = state.setdefault("stages", {})
    environment = {**os.environ, "BTO_COMBINED_NIGHTLY_EMAIL": "1"}
    for stage, command in commands:
        previous = stages.get(stage, {})
        if previous.get("status") == "complete":
            continue
        if previous.get("status") in {"running", "failed"} and stage != "sorting":
            raise RuntimeError("Verify interrupted " + stage + " before rerunning it")
        stages[stage] = {
            "status": "running", "started_at": timestamp(),
            "attempts": previous.get("attempts", 0) + 1,
        }
        state.update(status="running", stage=stage)
        save(path, state)
        outcome = invoke(command, env=environment, check=False)
        stages[stage].update(finished_at=timestamp(), exit_code=outcome.returncode)
        if outcome.returncode:
            stages[stage]["status"] = "failed"
            detail = getattr(outcome, "failure_detail", "")
            raise RuntimeError(f"{stage} exited {outcome.returncode}: " + (detail or "see its recorded error/journal"))
        if stage == "collection" and check_collection:
            try:
                check_collection()
            except Exception:
                stages[stage]["status"] = "failed"
                save(path, state)
                raise
        stages[stage]["status"] = "complete"
        save(path, state)
    state.pop("failed_stage", None)
    state.pop("error", None)
    state.update(status="complete", finished_at=timestamp())
    save(path, state)


def invoke(command, *, env, check):
    """Retain the existing upstream deadlines; Batch has no wall-clock cutoff."""
    import threading
    from collections import deque
    from types import SimpleNamespace

    timeout = 6 * 3600 if command[2:4] == ["bto", "collect"] else 4 * 3600 if "bto.clean_job_listings.run" in command else None
    tail = deque(maxlen=12)
    process = subprocess.Popen(command, env=env, stderr=subprocess.PIPE)

    def read_errors():
        for line in process.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
            tail.append(line.decode("utf-8", errors="replace").strip())

    reader = threading.Thread(target=read_errors, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        code = 124
        tail.append("The existing upstream process deadline was exhausted")
    reader.join(timeout=5)
    detail = "\n".join(tail)[-3000:]
    for key, value in env.items():
        if value and any(word in key for word in ("PASSWORD", "TOKEN", "API_KEY", "SECRET")):
            detail = detail.replace(value, "[private]")
    return SimpleNamespace(returncode=code, failure_detail=detail)


def select_state(previous, collection_night, invocation_id):
    """One same-night chain; unfinished sorting resumes before a later collection."""
    if previous and previous.get("collection_night") == collection_night and previous.get("status") == "complete":
        return previous
    if previous and previous.get("status") != "complete" and (
        previous.get("collection_night") == collection_night or previous.get("stage") == "sorting"
    ):
        previous["invocation_id"] = invocation_id
        return previous
    return {"run_id": uuid4().hex, "started_at": timestamp(), "collection_night": collection_night, "invocation_id": invocation_id, "status": "running"}


def main(argv=None):
    from datetime import datetime, timezone

    parser = argparse.ArgumentParser(description="Complete the nightly BTO chain")
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--allow-gemini", action="store_true")
    parser.add_argument("--send-email", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=Path("data/job_sorting"))
    args = parser.parse_args(argv)
    if not args.allow_gemini:
        parser.error("the nightly chain requires explicitly authorised --allow-gemini")
    from bto import settings
    from bto.send_notifications import notify

    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "nightly.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "nightly.json"
        previous = json.loads(path.read_text()) if path.exists() else None
        collection_night = str(night(datetime.now(timezone.utc)))
        state = select_state(previous, collection_night, os.environ.get("INVOCATION_ID"))
        if state.get("status") == "complete":
            return 0
        batch_directory = directory / "runs" / state["run_id"]
        # This checkpoint precedes configuration/import failures, so the systemd
        # fallback cannot mistake a previous night's email for this invocation.
        save(path, state)
        config = settings.load()
        commands = [
            ("collection", [sys.executable, "-m", "bto", "collect", "--trigger", "scheduled"]),
            ("cleaning", [sys.executable, "-m", "bto.clean_job_listings.run", "--stage", "nightly", "--engine", "databricks", "--wheel", args.wheel]),
            ("sorting", [sys.executable, "-m", "bto.filter_and_bucket_job_listings.run", "--mode", "night", "--allow-gemini", "--refresh-review", "--state-dir", str(batch_directory)]),
        ]

        def collection_health():
            from bto.storage.databricks.connection import Databricks

            dbx = Databricks(config["databricks_host"], config["databricks_token"], config["databricks_warehouse_id"])
            handoff, rows = notify.read_collection(dbx, config)
            state["collection_problems"] = [r for r in rows if notify.collection_problem(r)]
            if handoff.get("state") != "finished":
                raise RuntimeError("Collection did not write its finished handoff")
            # Normal completion retains the adapters' partial/failed outcomes.
            # Cleaning still processes the usable observations that landed.

        try:
            from bto.review_jds.refresh_review_cache import configuration

            configuration()
            execute(state, path, commands, invoke=invoke, check_collection=collection_health)
        except Exception as error:
            state.update(status="failed", failed_stage=state.get("stage", "startup"), error=str(error), finished_at=timestamp())
        batch_path = batch_directory / "batch.json"
        if not batch_path.exists():
            batch_path = batch_directory / "preflight-failure.json"
        if batch_path.exists():
            state["sorting"] = json.loads(batch_path.read_text())
            if state["status"] != "complete" and state["sorting"].get("error"):
                state.update(error=state["sorting"]["error"], failed_stage=state["sorting"].get("failed_stage", "sorting"))
        clean_path = directory / "cleaning-outcome.json"
        if clean_path.exists():
            clean = json.loads(clean_path.read_text())
            if clean["at"] >= state["started_at"]:
                state["cleaning"] = clean
                if clean.get("error") and state["status"] != "complete":
                    state["error"] = clean["error"]
        save(path, state)
        if not state.get("terminal_email_attempted_at"):
            subject, plain, markup = notify.render_terminal_nightly(state)
            if args.send_email:
                state["terminal_email_attempted_at"] = timestamp()
                save(path, state)
                state["terminal_email_delivered"] = notify.send_email(config, subject, plain, html=markup)
                save(path, state)
            else:
                (directory / "terminal_email.txt").write_text(plain)
                (directory / "terminal_email.html").write_text(markup)
        save(batch_directory / "nightly.json", state)
        return 0 if state["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
