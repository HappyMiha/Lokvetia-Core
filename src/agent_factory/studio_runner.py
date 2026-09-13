"""A bounded local queue for explicitly requested studio planning."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import sqlite3
import threading
import time

from .autonomous_mission import AutonomousMissionService
from .local_games import local_games_lock
from .storage import SQLiteStorage
from .studio_supervisor import CoreMissionDriver, Supervisor
from .studio_start import checked_local_source
from .localisation import Message


class StudioRunner:
    def __init__(self, database, workspace, *, driver_factory=CoreMissionDriver,
                 source_check=checked_local_source):
        self.database, self.workspace = database, workspace
        self.driver_factory = driver_factory
        self.source_check = source_check
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-local")
        self.guard = threading.Lock()
        self.jobs = {}
        self.cancellations = {}
        self.active = set()
        self.stopping = threading.Event()

    def submit(self, mission_id):
        with self.guard:
            previous = self.jobs.get(mission_id)
            if previous is not None and not previous.done():
                return False
            self.cancellations[mission_id] = threading.Event()
            self.jobs[mission_id] = self.executor.submit(self._run, mission_id)
            return True

    def _run(self, mission_id):
        # One inference stream even when two local web processes share this DB.
        # Unlike the execution lock, the state DB remains writable for Pause.
        with self._inference_turn(self.cancellations.get(mission_id)) as acquired:
            with closing(SQLiteStorage(self.database)) as storage:
                mission = AutonomousMissionService(storage).get(mission_id)
                if not acquired:
                    Supervisor(storage)._write(mission.mission_key, "plan", "waiting", Message(
                        "Локальна черга зупинена або час очікування моделі вичерпано. Планування не почалося.",
                        "The local queue stopped or its model wait expired. Planning did not start."))
                    return ()
                try:
                    self.source_check(storage, self.workspace)
                except Exception:
                    Supervisor(storage)._write(mission.mission_key, "plan", "waiting", Message(
                        "Локальна модель або її конфігурація змінилася. Повторіть перевірку воркера.",
                        "The local model or its configuration changed. Qualify the worker again."))
                    return ()
                with self.guard:
                    self.active.add(mission_id)
                try:
                    driver = self.driver_factory(storage, mission_id, workspace=self.workspace)
                    if isinstance(driver, CoreMissionDriver):
                        driver.cancel_event = self.cancellations[mission_id]
                    return Supervisor(storage).run(mission.mission_key, driver)
                finally:
                    with self.guard:
                        self.active.discard(mission_id)

    @contextmanager
    def _inference_turn(self, cancel_event=None):
        # A second web process can wait through a multi-minute inference. The
        # ordinary ten-second mutation lock must not silently drop its job.
        deadline = time.monotonic() + 3600
        while not self.stopping.is_set() and not (cancel_event and cancel_event.is_set()) and time.monotonic() < deadline:
            lock = local_games_lock(str(self.database) + ".studio-inference", timeout=0.25)
            try:
                lock.__enter__()
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                    raise
                self.stopping.wait(0.1)
                continue
            try:
                yield True
            finally:
                lock.__exit__(None, None, None)
            return
        yield False

    def status(self, mission_id):
        with self.guard:
            future = self.jobs.get(mission_id)
            if future is None:
                return "idle"
            if future.cancelled():
                return "cancelled"
            if not future.done():
                return "running" if mission_id in self.active else "queued"
            return "failed" if future.exception() is not None else "finished"

    def close(self):
        self.stopping.set()
        with self.guard:
            for event in self.cancellations.values(): event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def cancel(self, mission_id):
        with self.guard:
            event=self.cancellations.get(mission_id)
            if event: event.set()
            future=self.jobs.get(mission_id)
            if future: future.cancel()
