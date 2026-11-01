"""Run collection, cleaning, sorting and Review refresh as one nightly workflow.

Save each step's progress, resume eligible unfinished work and send one final
email describing completion or failure."""

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from bto.filter_and_bucket_job_listings import night
from bto.filter_and_bucket_job_listings.call_gemini_batch import (
    release_checkpoint_memory,
    save,
    timestamp,
)
from bto.log_errors import log_failure

log = logging.getLogger(__name__)


def execute(state, path, commands, *, invoke=subprocess.run, check_collection=None):
    """Run each unfinished stage once and save its outcome before continuing."""
    stages = state.setdefault("stages", {})
    environment = {**os.environ, "BTO_COMBINED_NIGHTLY_EMAIL": "1"}
    for stage, command in commands:
        previous = stages.get(stage, {})
        if previous.get("status") == "complete":
            log.info("Nightly stage retained run_id=%s stage=%s", state.get("run_id"), stage)
            continue
        if previous.get("status") in {"running", "failed"} and stage != "sorting":
            log.error("Nightly stage needs inspection run_id=%s stage=%s checkpoint=%s",
                      state.get("run_id"), stage, path)
            raise RuntimeError("Verify interrupted " + stage + " before rerunning it")
        stages[stage] = {
            "status": "running", "started_at": timestamp(),
            "attempts": previous.get("attempts", 0) + 1,
        }
        if state.get("backfill") and stage in ("collection", "cleaning"):
            steps = state.setdefault("backfill_steps", [])
            if stage not in steps:
                steps.append(stage)
        state.update(status="running", stage=stage)
        save(path, state)
        started = time.monotonic()
        log.info("Nightly stage started run_id=%s stage=%s attempt=%s",
                 state.get("run_id"), stage, stages[stage]["attempts"])
        outcome = invoke(command, env=environment)
        stages[stage].update(finished_at=timestamp(), exit_code=outcome.returncode)
        if outcome.returncode:
            stages[stage]["status"] = "failed"
            log.error("Nightly stage failed run_id=%s stage=%s exit_code=%s seconds=%.1f checkpoint=%s",
                      state.get("run_id"), stage, outcome.returncode,
                      time.monotonic() - started, path)
            detail = getattr(outcome, "failure_detail", "")
            raise RuntimeError(f"{stage} exited {outcome.returncode}: " + (detail or "see its recorded error/journal"))
        if stage == "collection" and check_collection:
            try:
                check_collection()
            except Exception as error:
                stages[stage]["status"] = "failed"
                save(path, state)
                log_failure(log, "Collection handoff rejected run_id=%s checkpoint=%s",
                            state.get("run_id"), path, error=error)
                raise
        stages[stage]["status"] = "complete"
        save(path, state)
        log.info("Nightly stage completed run_id=%s stage=%s seconds=%.1f",
                 state.get("run_id"), stage, time.monotonic() - started)
    state.pop("failed_stage", None)
    state.pop("error", None)
    state.update(status="complete", finished_at=timestamp())
    save(path, state)


def invoke(command, *, env):
    """Stream child errors and enforce collection and cleaning deadlines."""
    import threading
    from collections import deque
    from types import SimpleNamespace

    timeout = None
    if command[2:4] == ["bto", "collect"]:
        timeout = 6 * 3600
    elif "bto.clean_job_listings.run" in command:
        timeout = 4 * 3600
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
    """Resume unfinished sorting before starting another collection night."""
    if previous and previous.get("collection_night") == collection_night and (
        previous.get("status") == "complete"
    ):
        if not previous.get("terminal_email_delivered"):
            previous["invocation_id"] = invocation_id
        return previous
    if previous and previous.get("status") != "complete" and (
        previous.get("collection_night") == collection_night or previous.get("stage") == "sorting"
    ):
        if previous.get("terminal_email_delivered"):
            previous["backfill"] = True
            previous.setdefault("backfill_started_at", timestamp())
            previous.setdefault("backfill_steps", [])
            previous.setdefault("prior_failure", {
                "stage": previous.get("failed_stage"),
                "finished_at": previous.get("finished_at"),
                "email_at": previous.get("terminal_email_attempted_at"),
            })
            previous.pop("terminal_email_delivered", None)
            previous.pop("terminal_email_attempted_at", None)
        previous["invocation_id"] = invocation_id
        return previous
    return {
        "run_id": uuid4().hex, "started_at": timestamp(),
        "collection_night": collection_night, "invocation_id": invocation_id,
        "status": "running",
    }


def check_private_configuration():
    """Validate preferences and both prompts without storage or provider calls."""
    from bto.filter_and_bucket_job_listings.config import (
        load_preferences,
        load_prompt,
        normal_night_ceiling,
        processing_start,
    )

    preferences = load_preferences()
    processing_start()
    normal_night_ceiling()
    for stage in ("triage", "extraction"):
        load_prompt(stage, preferences)


def main(argv=None):
    from datetime import datetime, timezone

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Complete the nightly BTO chain")
    parser.add_argument("--wheel")
    parser.add_argument("--check-config", action="store_true",
                        help="validate private preferences and prompts, then exit without running jobs")
    parser.add_argument("--allow-gemini", action="store_true")
    parser.add_argument("--send-email", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=Path("data/job_sorting"))
    args = parser.parse_args(argv)
    if args.check_config:
        check_private_configuration()
        log.info("Private sorting preferences and prompts validated")
        return 0
    if not args.wheel:
        parser.error("--wheel is required to run the nightly workflow")
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
        del previous
        log.info("Nightly checkpoint selected run_id=%s collection_night=%s status=%s checkpoint=%s",
                 state.get("run_id"), state["collection_night"], state["status"], path)
        if state.get("status") == "complete" and state.get("terminal_email_delivered"):
            return 0
        batch_directory = directory / "runs" / state["run_id"]
        if state.get("status") != "complete":
            # Sorting reloads its authoritative checkpoint after execution.
            state.pop("sorting", None)
        release_checkpoint_memory()
        # This checkpoint precedes configuration/import failures, so the systemd
        # fallback cannot mistake a previous night's email for this invocation.
        save(path, state)
        config = settings.load()
        commands = [
            ("collection", [sys.executable, "-m", "bto", "collect", "--trigger", "scheduled"]),
            ("cleaning", [
                sys.executable, "-m", "bto.clean_job_listings.run", "--stage", "nightly",
                "--engine", "databricks", "--wheel", args.wheel,
            ]),
            ("sorting", [
                sys.executable, "-m", "bto.filter_and_bucket_job_listings.run", "--mode", "night",
                "--allow-gemini", "--refresh-review", "--state-dir", str(batch_directory),
            ]),
        ]

        def collection_health():
            from bto.storage.databricks.connection import Databricks

            dbx = Databricks(config["databricks_host"], config["databricks_token"], config["databricks_warehouse_id"])
            handoff, rows = notify.read_collection(dbx, config)
            state["collection_rows"] = rows
            state["collection_problems"] = [r for r in rows if notify.collection_problem(r)]
            if handoff.get("state") != "finished":
                raise RuntimeError("Collection did not write its finished handoff")
            # Normal completion retains the adapters' partial/failed outcomes.
            # Cleaning still processes the usable observations that landed.

        try:
            from bto.review_jds.refresh_review_cache import configuration

            if state.get("status") != "complete":
                configuration()
                check_private_configuration()
                execute(state, path, commands, invoke=invoke, check_collection=collection_health)
        except Exception as error:
            from bto.filter_and_bucket_job_listings.config import PrivateConfigurationError
            from bto.send_notifications.nightly_email import error_explanation

            state.update(status="failed", failed_stage=state.get("stage", "startup"), error=str(error), finished_at=timestamp())
            reason = (str(error) if isinstance(error, PrivateConfigurationError)
                      else error_explanation(error) or "See saved checkpoint")
            log_failure(log, "Nightly run failed run_id=%s stage=%s reason=%s checkpoint=%s",
                        state.get("run_id"), state["failed_stage"], reason, path, error=error)
        batch_path = batch_directory / "batch.json"
        if not batch_path.exists():
            batch_path = batch_directory / "preflight-failure.json"
        if batch_path.exists():
            state["sorting"] = json.loads(batch_path.read_text())
            if state["status"] != "complete" and state["sorting"].get("error"):
                state.update(error=state["sorting"]["error"], failed_stage=state["sorting"].get("failed_stage", "sorting"))
        # Cleaning records its outcome relative to the shared working directory.
        clean_path = Path("data/job_sorting/cleaning-outcome.json")
        cleaning = state.get("stages", {}).get("cleaning", {})
        if clean_path.exists() and cleaning.get("started_at"):
            clean = json.loads(clean_path.read_text())
            if cleaning["started_at"] <= clean["at"] <= cleaning.get("finished_at", timestamp()):
                state["cleaning"] = clean
                if clean.get("error") and state.get("failed_stage") == "cleaning":
                    state["error"] = clean["error"]
        from bto.send_notifications.nightly_email_data import collect_collection_cost

        collect_collection_cost(state, config)
        save(path, state)
        if not state.get("terminal_email_delivered"):
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
        log.info("Nightly run finished run_id=%s status=%s terminal_email_delivered=%s",
                 state.get("run_id"), state["status"], bool(state.get("terminal_email_delivered")))
        return 0 if state["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
