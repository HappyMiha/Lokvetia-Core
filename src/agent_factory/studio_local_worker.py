"""Register this PC and qualify an installed local model without sending keys.

Run explicitly on the worker host. No downloads, remote endpoints or server
credentials are accepted. A hardware fit alone never marks a model verified.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import socket
import sys

from .godot_engine import GodotAdapter
from .hardware_inventory import collect_inventory
from .local_role_qualification import qualify
from .localisation import Message
from .machine_identity import require_a_build_machine
from .storage import SQLiteStorage
from .studio_first_run import FirstRun
from .studio_workers import StudioMachines


def setup(storage, workspace, *, actor, model="qwen2.5-coder:7b", godot="godot",
          run_live=False, collector=collect_inventory, qualifier=qualify):
    machine = require_a_build_machine("Local worker setup")
    if not machine.is_the_users_computer:
        raise ValueError("Run local worker setup on your PC")
    if not actor.strip():
        raise ValueError("Worker setup needs an operator")
    required = {"qwen2.5-coder:7b": 6.0, "qwen2.5-coder:14b": 12.0}
    if model not in required:
        raise ValueError("Unsupported local model")
    inventory = collector(Path(workspace))
    # A model must fit one card; summing unrelated GPUs invents pooled memory.
    memory = max((gpu.get("dedicated_total_bytes") or 0 for gpu in inventory["gpus"]), default=0)
    machine_key = "local-" + socket.gethostname().lower()
    machines = StudioMachines(storage)
    health = GodotAdapter(executable_candidates=(godot,)).health()
    machines.register(machine_key, name=socket.gethostname(), kind="this_pc",
                      capabilities=["godot"] if health.healthy else [],
                      video_memory_gb=memory / 1024 ** 3, registered_by=actor)
    machines.report(machine_key, kind="hardware_inventory", body=inventory)
    wizard = FirstRun(storage)
    verdict = machines.local_model_fits(machine_key, needed_gb=required[model])
    detail = Message("Модель ще не перевірено запитом.", "The model has not passed an inference check.")
    source = wizard.connect("ollama", kind="local_model", name=model,
                            state="unverified" if verdict.fits else "unavailable",
                            detail=detail if verdict.fits else verdict.reason,
                            machine_key=machine_key, connected_by=actor)
    evidence = None
    if run_live and verdict.fits:
        try:
            evidence = qualifier(model)
            if not evidence.get("results") or not all(row.get("passed") is True for row in evidence["results"]):
                raise ValueError("Local qualification failed")
        except Exception:
            wizard.verify("ollama", works=False, detail=Message(
                "Перевірка локальної моделі не пройшла. Перевірте Ollama та повторіть налаштування.",
                "Local model qualification failed. Check Ollama and repeat setup."))
            raise
        machines.report(machine_key, kind="local_model_qualification", body=evidence)
        source = wizard.verify("ollama", works=True, detail=Message(
            "Модель пройшла перевірку памʼяті та реальних запитів API і CLI. Це ще не доказ створення гри.",
            "The model passed memory and real API/CLI checks. This does not prove game generation."))
    return {"machine": machines.machine(machine_key).record("uk"),
            "source": source.record("uk"), "godot_available": health.healthy,
            "qualification": evidence, "can_start": wizard.readiness().can_start}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--godot", default="godot")
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args(argv)
    root = args.workspace.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with closing(SQLiteStorage(root / ".agent-factory" / "state.db")) as storage:
        result = setup(storage, root, actor=args.actor, model=args.model,
                       godot=args.godot, run_live=args.run_live)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["can_start"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
