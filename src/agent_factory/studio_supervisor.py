"""The thing that actually starts the work, and keeps starting it.

Everything else in the studio decides *whether* something may happen and
records that it did. Nothing moved a mission by itself: a game idea became a
mission in `DRAFT` and stayed there, because no supervisor, daemon or workflow
in this product ever picked it up. The pieces to advance it all existed —
planning, the gates, the money, the workers — with no wire from "a person
pressed Create" to "the work began".

This is that wire, and it is deliberately narrow.

**Nothing runs without a mandate.** Every mission-level act in Core demands the
mission owner's name. A supervisor that simply signed that name would forge the
owner's consent on every automatic step. So a named person records, once, what
the studio may do on this game without asking again: which steps, up to how much
money, until when. Each automatic step then carries both — the owner's name,
because the work is theirs, and the mandate that made signing it honest. With no
live mandate the supervisor does nothing at all and says so; it never guesses
what a person would have allowed.

**One step per call.** `advance()` performs at most one thing and returns what it
did or why it did not. A loop over it is a runner, not a runaway: it can be
stopped between steps, it is the same code in a test as in production, and a
crash can lose at most one step.

**A step that was in flight is never relaunched.** Work that was interrupted may
already have been paid for and may already have happened. On restart such a step
becomes `unknown` and waits for a person, exactly as an interrupted run does
elsewhere in the studio.

What this module does not do: it does not plan, execute or build anything
itself. It asks the gates, checks the money, and calls whoever does the work.
"""

from __future__ import annotations

import json
import math
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence, TYPE_CHECKING

from .localisation import DEFAULT_LANGUAGE, LocalisedError, Message, normalise, verbatim

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .storage import SQLiteStorage

# What a mandate may cover, in the order the work needs them.
STEPS = ("plan", "approve_plan")
# Which gate in the autonomy catalogue each step answers.
STEP_GATES = {"plan": "execution_authorization", "approve_plan": "backlog_revision"}
OUTCOMES = (
    "in_flight",     # started, not yet finished; only a restart leaves one behind
    "advanced",      # the mission moved
    "waiting",       # something outside the studio is not ready yet
    "asked",         # a person has been asked, and the work waits on the answer
    "refused",       # the studio may not do this, and says which rule stopped it
    "nothing_to_do",  # the mission is not at a step this supervisor knows
    "unknown",       # interrupted; a person has to say what happened
)
FINISHED = tuple(outcome for outcome in OUTCOMES if outcome != "in_flight")
DEFAULT_HOURS = 24


class SupervisorRefused(LocalisedError):
    """Raised when starting work would claim something that is not true."""


NO_STEPS = Message(
    "Доручення має називати принаймні один крок.",
    "A mandate has to name at least one step.",
)
UNKNOWN_STEP = Message(
    "Невідомий крок: {step}.", "Unknown step: {step}.",
)
NAME_REQUIRED = Message(
    "Доручення дає конкретна людина, тому потрібне імʼя.",
    "A mandate is given by a particular person, so a name is required.",
)
HOURS_REQUIRED = Message(
    "Доручення закінчується. Термін має бути додатним.",
    "A mandate runs out. Its term has to be positive.",
)
INVALID_MONEY = Message("Ліміт і вартість мають бути скінченними невідʼємними числами.",
                        "Limits and costs must be finite non-negative numbers.")
WRONG_OWNER = Message("Доручення має належати власнику цієї гри.",
                      "The mandate must belong to this game's owner.")
WRONG_MISSION = Message("Доручення стосується іншої гри.", "The mandate belongs to a different game.")
PAUSED = Message("Гру призупинено. Нові задачі не починаються.",
                 "The game is paused. No new tasks will start.")
NOT_RUNNING = Message("Місія не виконується: {state}.", "The mission is not running: {state}.")
UNDER_WAY = Message(
    "Крок триває.", "The step is under way.",
)
CANNOT_APPROVE_YET = Message(
    "Студія ще не затверджує план сама: затвердження прив’язує роботу до "
    "конкретного коміту у виписаному репозиторії й до виконавця, якого поки "
    "немає. Тому цей крок лишається за людиною.",
    "The studio does not approve a plan on its own yet: approving binds the "
    "work to one commit in a checked-out repository and to an executor that "
    "does not exist yet. So this step stays with a person.",
)
PLAN_NOT_READY = Message(
    "План не пройшов власних перевірок: зауважень — {count}. Студія не подає "
    "його на затвердження, поки на нього не гляне людина.",
    "The plan did not pass its own checks: {count} finding(s). The studio does "
    "not put it up for approval until a person has looked at it.",
)
DID_NOT_MOVE = Message(
    "Крок «{step}» відпрацював, але місія лишилася там, де була ({phase}). "
    "Студія не повторює його наосліп.",
    "The step “{step}” ran, but the mission stayed where it was ({phase}). The "
    "studio does not repeat it blindly.",
)
NOTHING_TO_REVOKE = Message(
    "Тут немає чинного доручення, яке можна відкликати.",
    "There is no live mandate here to revoke.",
)

NO_MANDATE = Message(
    "Ніхто не доручив студії вести цю гру самостійно, тому вона нічого не робить.",
    "Nobody has asked the studio to run this game on its own, so it does nothing.",
)
MANDATE_EXPIRED = Message(
    "Доручення закінчилося {at}. Робота стоїть, доки його не поновлять.",
    "The mandate ran out on {at}. The work waits until it is renewed.",
)
STEP_NOT_COVERED = Message(
    "Доручення не покриває крок «{step}», тому це рішення лишається за людиною.",
    "The mandate does not cover the step “{step}”, so this one stays with "
    "a person.",
)
NO_SOURCE = Message(
    "Немає перевіреного джерела виконання, тому починати нема з чим.",
    "There is no checked source of execution, so there is nothing to start with.",
)
BLOCKED_BY_QUESTION = Message(
    "Робота чекає на відповідь: відкритих питань — {count}.",
    "The work is waiting on an answer: {count} open question(s).",
)
OVER_CEILING = Message(
    "Наступний крок коштує {amount} {unit}, а доручення дозволяє ще {remaining}.",
    "The next step costs {amount} {unit}, and the mandate allows {remaining} more.",
)
OVER_LIMIT = Message(
    "Наступний крок виходить за ліміт витрат цієї гри.",
    "The next step goes past this game's spending limit.",
)
NOT_MY_STEP = Message(
    "Місія не на тому кроці, який веде студія: {phase}.",
    "The mission is not at a step this studio drives: {phase}.",
)
PLANNED = Message(
    "План робіт складено. Далі — затвердити його.",
    "The plan of work has been drawn up. Approving it comes next.",
)
APPROVED = Message(
    "План затверджено за дорученням; роботу дозволено.",
    "The plan was approved under the mandate; the work is allowed to run.",
)
STEP_FAILED = Message(
    "Крок «{step}» не вдався: {reason}",
    "The step “{step}” failed: {reason}",
)
INTERRUPTED = Message(
    "Крок «{step}» перервано. Він міг статися й міг бути оплачений, тому студія "
    "його не повторює — скажіть, що з ним робити.",
    "The step “{step}” was interrupted. It may have happened and may have "
    "been paid for, so the studio does not repeat it — say what to do with it.",
)
GRANTED = Message(
    "{who} доручив(ла) студії вести цю гру самостійно.",
    "{who} asked the studio to run this game on its own.",
)
REVOKED = Message(
    "Доручення відкликано. Студія більше нічого не починає сама.",
    "The mandate was revoked. The studio starts nothing more on its own.",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(stamp: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(stamp))
        return value if value.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _later(left: str, right: str) -> bool:
    """True when `left` is after `right`, treating an unreadable stamp as past."""
    first, second = _parse(left), _parse(right)
    return bool(first and second and first > second)


@dataclass(frozen=True)
class Mandate:
    """What a named person allowed the studio to do here without asking again."""

    mission: str
    granted_by: str
    steps: tuple[str, ...]
    granted_at: str
    expires_at: str
    ceiling: float | None = None
    unit: str = "USD"
    reason: str = ""
    revoked_at: str = ""
    revoked_by: str = ""
    identifier: int = 0

    def live(self, *, at: str = "") -> bool:
        if self.revoked_at:
            return False
        now, start, end = _parse(at or _now()), _parse(self.granted_at), _parse(self.expires_at)
        return bool(now and start and end and start <= now < end)

    def covers(self, step: str) -> bool:
        return step in self.steps

    def record(self, language: str = DEFAULT_LANGUAGE) -> dict[str, Any]:
        chosen = normalise(language)
        return {
            "mission": self.mission,
            "granted_by": self.granted_by,
            "steps": list(self.steps),
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "ceiling": self.ceiling,
            "unit": self.unit,
            "reason": self.reason,
            "revoked_at": self.revoked_at,
            "revoked_by": self.revoked_by,
            "live": self.live(),
            "summary": Message(
                GRANTED.uk.format(who=self.granted_by),
                GRANTED.en.format(who=self.granted_by),
            ).text(chosen) if not self.revoked_at else REVOKED.text(chosen),
        }


@dataclass(frozen=True)
class Step:
    """One thing the supervisor did here, or declined to do, and why."""

    mission: str
    step: str
    outcome: str
    summary: Message
    started_at: str
    finished_at: str = ""
    granted_by: str = ""
    actor: str = ""
    detail: Message | None = None
    identifier: int = 0

    @property
    def moved(self) -> bool:
        return self.outcome == "advanced"

    @property
    def needs_person(self) -> bool:
        return self.outcome in {"asked", "unknown"}

    def record(self, language: str = DEFAULT_LANGUAGE) -> dict[str, Any]:
        chosen = normalise(language)
        return {
            "step": self.step,
            "outcome": self.outcome,
            "moved": self.moved,
            "needs_person": self.needs_person,
            "summary": self.summary.text(chosen),
            "detail": self.detail.text(chosen) if self.detail else "",
            "granted_by": self.granted_by,
            "actor": self.actor,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class MissionState:
    """The little a supervisor needs to know about a mission to drive it."""

    mission_id: int
    owner: str
    phase: str
    disposition: str
    version: int
    mission_key: str = ""


@dataclass(frozen=True)
class Advanced:
    """What performing a step produced."""

    phase: str
    detail: Message | None = None
    cost: float = 0.0


class MissionDriver(Protocol):
    """Whoever can actually move one mission. Kept behind a protocol so this
    module stays testable without a provider, and so the heavy Core services are
    imported only where they are used."""

    def state(self) -> MissionState: ...

    def plan(self, *, actor: str, command_id: str) -> Advanced: ...

    def approve(self, *, actor: str, command_id: str) -> Advanced: ...


PLANNING_PHASES = ("DRAFT", "SPECIFICATION_ANALYSIS", "BACKLOG_GENERATION")
APPROVING_PHASES = ("WAITING_FOR_BACKLOG_APPROVAL",)


def next_step(phase: str) -> str:
    """Which step a mission in this phase is waiting for, if any."""
    if phase in PLANNING_PHASES:
        return "plan"
    if phase in APPROVING_PHASES:
        return "approve_plan"
    return ""


SUPERVISOR_MIGRATION = """
CREATE TABLE studio_mandates(
    id INTEGER PRIMARY KEY,
    mission TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    ceiling REAL,
    unit TEXT NOT NULL DEFAULT 'USD',
    reason TEXT NOT NULL DEFAULT '',
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL DEFAULT '',
    revoked_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_studio_mandates_mission ON studio_mandates(mission, granted_at DESC);
CREATE TRIGGER studio_mandates_are_immutable BEFORE UPDATE ON studio_mandates
WHEN OLD.mission <> NEW.mission OR OLD.granted_by <> NEW.granted_by
  OR OLD.steps_json <> NEW.steps_json OR OLD.granted_at <> NEW.granted_at
  OR OLD.expires_at <> NEW.expires_at OR OLD.unit <> NEW.unit
  OR IFNULL(OLD.ceiling, -1) <> IFNULL(NEW.ceiling, -1)
  OR (OLD.revoked_at <> '' AND OLD.revoked_at <> NEW.revoked_at)
BEGIN
    SELECT RAISE(ABORT, 'a mandate may only gain its revocation');
END;
CREATE TABLE studio_supervisor_steps(
    id INTEGER PRIMARY KEY,
    mission TEXT NOT NULL,
    step TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN
        ('in_flight','advanced','waiting','asked','refused','nothing_to_do','unknown')),
    summary_uk TEXT NOT NULL,
    summary_en TEXT NOT NULL,
    detail_uk TEXT NOT NULL DEFAULT '',
    detail_en TEXT NOT NULL DEFAULT '',
    mandate_id INTEGER REFERENCES studio_mandates(id),
    granted_by TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    command_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_studio_supervisor_steps_mission
    ON studio_supervisor_steps(mission, id DESC);
CREATE TRIGGER studio_supervisor_steps_finish_once BEFORE UPDATE
ON studio_supervisor_steps
WHEN OLD.outcome <> 'in_flight'
BEGIN
    SELECT RAISE(ABORT, 'a finished step is not rewritten');
END;
"""


class Supervisor:
    """Starts the work a mandate allows, one step at a time, and records it."""

    def __init__(self, storage: "SQLiteStorage"):
        self.storage = storage

    # ------------------------------------------------------------- mandates

    def grant(
        self,
        mission: str,
        *,
        steps: Sequence[str],
        granted_by: str,
        ceiling: float | None = None,
        unit: str = "USD",
        hours: float = DEFAULT_HOURS,
        reason: str = "",
        at: str = "",
    ) -> Mandate:
        """Record what one named person allows here. Supersedes, never edits."""
        who = str(granted_by).strip()
        if not who:
            raise SupervisorRefused(NAME_REQUIRED)
        wanted = tuple(dict.fromkeys(str(step).strip() for step in steps if str(step).strip()))
        if not wanted:
            raise SupervisorRefused(NO_STEPS)
        for step in wanted:
            if step not in STEPS:
                raise SupervisorRefused(UNKNOWN_STEP, step=step)
        if not math.isfinite(float(hours)) or not 0 < float(hours) <= 8760:
            raise SupervisorRefused(HOURS_REQUIRED)
        if ceiling is not None and (not math.isfinite(float(ceiling)) or float(ceiling) < 0):
            raise SupervisorRefused(INVALID_MONEY)
        granted_at = at or _now()
        starts = _parse(granted_at) or datetime.now(timezone.utc)
        expires = (starts + timedelta(hours=float(hours))).isoformat(timespec="seconds")
        live = self.mandate(mission, at=granted_at)
        with self.storage.db:
            if live is not None:
                # A superseded mandate is closed, not overwritten: the record has to
                # keep saying that it once existed and when it stopped applying.
                self.storage.db.execute(
                    "UPDATE studio_mandates SET revoked_at=?, revoked_by=? WHERE id=?",
                    (granted_at, who, live.identifier),
                )
            cursor = self.storage.db.execute(
                """INSERT INTO studio_mandates
                   (mission,granted_by,steps_json,ceiling,unit,reason,granted_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (str(mission), who, json.dumps(list(wanted)),
                 None if ceiling is None else float(ceiling), str(unit), str(reason),
                 granted_at, expires),
            )
        return Mandate(str(mission), who, wanted, granted_at, expires,
                       None if ceiling is None else float(ceiling), str(unit),
                       str(reason), identifier=int(cursor.lastrowid))

    def revoke(self, mission: str, *, actor: str, at: str = "") -> Mandate:
        """Stop the studio starting anything else here, without erasing what it did."""
        who = str(actor).strip()
        if not who:
            raise SupervisorRefused(NAME_REQUIRED)
        stamp = at or _now()
        live = self.mandate(mission, at=stamp)
        if live is None:
            raise SupervisorRefused(NOTHING_TO_REVOKE)
        with self.storage.db:
            self.storage.db.execute(
                "UPDATE studio_mandates SET revoked_at=?, revoked_by=? WHERE id=?",
                (stamp, who, live.identifier),
            )
        return Mandate(live.mission, live.granted_by, live.steps, live.granted_at,
                       live.expires_at, live.ceiling, live.unit, live.reason,
                       stamp, who, live.identifier)

    def _mandate_row(self, row: Any) -> Mandate:
        return Mandate(
            row["mission"], row["granted_by"], tuple(json.loads(row["steps_json"])),
            row["granted_at"], row["expires_at"], row["ceiling"], row["unit"],
            row["reason"], row["revoked_at"], row["revoked_by"], int(row["id"]),
        )

    def mandate(self, mission: str, *, at: str = "") -> Mandate | None:
        """The mandate in force here, or nothing. An expired one is not in force."""
        row = self.storage.db.execute(
            "SELECT * FROM studio_mandates WHERE mission=? AND revoked_at='' "
            "ORDER BY id DESC LIMIT 1", (str(mission),)).fetchone()
        if row is None:
            return None
        # An expired mandate is still returned, so the caller can say that it
        # expired rather than that nobody ever gave one.
        return self._mandate_row(row)

    def mandates(self, mission: str) -> tuple[Mandate, ...]:
        rows = self.storage.db.execute(
            "SELECT * FROM studio_mandates WHERE mission=? ORDER BY id DESC",
            (str(mission),)).fetchall()
        return tuple(self._mandate_row(row) for row in rows)

    # ---------------------------------------------------------------- steps

    def _open(self, mission: str, step: str, *, mandate: Mandate | None,
              actor: str, command_id: str, at: str) -> int:
        with self.storage.db:
            cursor = self.storage.db.execute(
                """INSERT INTO studio_supervisor_steps
                   (mission,step,outcome,summary_uk,summary_en,mandate_id,granted_by,
                    actor,command_id,started_at)
                   VALUES(?,?,'in_flight',?,?,?,?,?,?,?)""",
                (str(mission), step, "", "",
                 mandate.identifier if mandate else None,
                 mandate.granted_by if mandate else "", actor, command_id, at),
            )
        return int(cursor.lastrowid)

    def _close(self, identifier: int, outcome: str, summary: Message,
               detail: Message | None, at: str) -> None:
        with self.storage.db:
            self.storage.db.execute(
                """UPDATE studio_supervisor_steps
                   SET outcome=?, summary_uk=?, summary_en=?, detail_uk=?, detail_en=?,
                       finished_at=? WHERE id=?""",
                (outcome, summary.uk, summary.en,
                 detail.uk if detail else "", detail.en if detail else "",
                 at, identifier),
            )

    def _write(self, mission: str, step: str, outcome: str, summary: Message, *,
               mandate: Mandate | None = None, actor: str = "",
               detail: Message | None = None, at: str = "") -> Step:
        """Record a step that finished without anything being started.

        A fact is declared once. A runner asking every half minute why it cannot
        start would otherwise write the same sentence into the ledger forever, so
        an answer identical to the last one returns that record instead of adding
        another. The history stays a history of changes.
        """
        stamp = at or _now()
        latest = self.history(mission, limit=1)
        if latest and (latest[0].step, latest[0].outcome, latest[0].summary) == (
                step, outcome, summary):
            return latest[0]
        identifier = self._open(mission, step, mandate=mandate, actor=actor,
                                command_id="", at=stamp)
        self._close(identifier, outcome, summary, detail, stamp)
        return Step(str(mission), step, outcome, summary, stamp, stamp,
                    mandate.granted_by if mandate else "", actor, detail, identifier)

    def _step_row(self, row: Any) -> Step:
        detail = Message(row["detail_uk"], row["detail_en"]) if row["detail_uk"] else None
        return Step(
            row["mission"], row["step"], row["outcome"],
            Message(row["summary_uk"], row["summary_en"]) if row["summary_uk"]
            else UNDER_WAY,
            row["started_at"], row["finished_at"], row["granted_by"], row["actor"],
            detail, int(row["id"]),
        )

    def history(self, mission: str, *, limit: int = 50) -> tuple[Step, ...]:
        rows = self.storage.db.execute(
            "SELECT * FROM studio_supervisor_steps WHERE mission=? ORDER BY id DESC "
            "LIMIT ?", (str(mission), max(1, int(limit)))).fetchall()
        return tuple(self._step_row(row) for row in rows)

    def in_flight(self, mission: str) -> tuple[Step, ...]:
        rows = self.storage.db.execute(
            "SELECT * FROM studio_supervisor_steps WHERE mission=? AND "
            "outcome='in_flight' ORDER BY id", (str(mission),)).fetchall()
        return tuple(self._step_row(row) for row in rows)

    def reconcile(self, mission: str, *, at: str = "") -> tuple[Step, ...]:
        """After a restart: nothing in flight is ever repeated.

        An interrupted step may already have run and may already have been paid
        for. Repeating it to "make sure" is how a person is charged twice, so it
        becomes a question instead.
        """
        stamp = at or _now()
        closed = []
        for step in self.in_flight(mission):
            summary = Message(
                INTERRUPTED.uk.format(step=step.step),
                INTERRUPTED.en.format(step=step.step),
            )
            self._close(step.identifier, "unknown", summary, None, stamp)
            closed.append(Step(step.mission, step.step, "unknown", summary,
                               step.started_at, stamp, step.granted_by, step.actor,
                               None, step.identifier))
        return tuple(closed)

    # -------------------------------------------------------------- driving

    def advance(self, mission: str, driver: MissionDriver, *,
                next_step_cost: float = 0.0, at: str = "") -> Step:
        """Serialize a mission across HTTP, CLI and separate worker processes.

        Keep the execution lock separate from state.db so Pause can be recorded
        while a model is answering. Only the lock holder may reconcile a crash.
        """
        from .local_games import local_games_lock
        key = hashlib.sha256(str(mission).encode()).hexdigest()[:24]
        try:
            with local_games_lock(str(self.storage.path) + ".studio-" + key):
                return self._advance(mission, driver, next_step_cost=next_step_cost, at=at)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower():
                raise
            running = self.in_flight(str(mission))
            if running:
                return running[-1]
            raise

    def _advance(
        self,
        mission: str,
        driver: MissionDriver,
        *,
        next_step_cost: float = 0.0,
        at: str = "",
    ) -> Step:
        """Do at most one thing here, and say what was done or why it was not.

        The order of the refusals below is the order in which a person would want
        to hear them: who allowed this, is there anything to work with, is
        somebody already being asked something, does it fit the money, and only
        then what the mission actually needs next.
        """
        from .studio_autonomy import AutonomyJournal, decide, over_budget
        from .studio_cost import StudioCosts
        from .studio_first_run import FirstRun
        from .studio_cycles import StudioCycles

        if not math.isfinite(float(next_step_cost)) or float(next_step_cost) < 0:
            raise SupervisorRefused(INVALID_MONEY)

        stamp = at or _now()
        mission = str(mission)
        interrupted = self.reconcile(mission, at=stamp)
        if interrupted:
            return interrupted[-1]
        # A later poll must not silently retry the ambiguous work just reconciled.
        unresolved = self.storage.db.execute(
            "SELECT * FROM studio_supervisor_steps WHERE mission=? AND outcome='unknown' "
            "ORDER BY id DESC LIMIT 1", (mission,),
        ).fetchone()
        if unresolved is not None:
            return self._step_row(unresolved)

        mandate = self.mandate(mission, at=stamp)
        if mandate is None:
            return self._write(mission, "", "refused", NO_MANDATE, at=stamp)
        if not mandate.live(at=stamp):
            return self._write(
                mission, "", "waiting",
                Message(MANDATE_EXPIRED.uk.format(at=mandate.expires_at),
                        MANDATE_EXPIRED.en.format(at=mandate.expires_at)),
                mandate=mandate, at=stamp)

        readiness = FirstRun(self.storage).readiness()
        if not readiness.can_start:
            return self._write(mission, "", "waiting", NO_SOURCE, mandate=mandate,
                               detail=readiness.summary, at=stamp)

        state = driver.state()
        if state.mission_key and state.mission_key != mission:
            return self._write(mission, "", "refused", WRONG_MISSION, mandate=mandate, at=stamp)
        if mandate.granted_by != state.owner:
            return self._write(mission, "", "refused", WRONG_OWNER, mandate=mandate, at=stamp)
        if StudioCycles(self.storage).paused(mission):
            return self._write(mission, "", "waiting", PAUSED, mandate=mandate, at=stamp)
        if state.disposition != "RUNNING":
            return self._write(mission, "", "waiting", Message(
                NOT_RUNNING.uk.format(state=state.disposition),
                NOT_RUNNING.en.format(state=state.disposition)), mandate=mandate, at=stamp)

        journal = AutonomyJournal(self.storage)
        open_questions = journal.open_questions(mission=mission)
        if open_questions:
            return self._write(
                mission, "", "waiting",
                Message(BLOCKED_BY_QUESTION.uk.format(count=len(open_questions)),
                        BLOCKED_BY_QUESTION.en.format(count=len(open_questions))),
                mandate=mandate, at=stamp)

        step = next_step(state.phase)
        if not step:
            return self._write(
                mission, "", "nothing_to_do",
                Message(NOT_MY_STEP.uk.format(phase=state.phase),
                        NOT_MY_STEP.en.format(phase=state.phase)),
                mandate=mandate, at=stamp)
        if not mandate.covers(step):
            return self._write(
                mission, step, "refused",
                Message(STEP_NOT_COVERED.uk.format(step=step),
                        STEP_NOT_COVERED.en.format(step=step)),
                mandate=mandate, at=stamp)

        costs = StudioCosts(self.storage)
        money = costs.check(mission, next_step=next_step_cost)
        spent_under_mandate = float(money["committed"])
        if mandate.ceiling is not None and (
                spent_under_mandate + max(0.0, float(next_step_cost)) > mandate.ceiling):
            remaining = max(0.0, mandate.ceiling - spent_under_mandate)
            question = over_budget(amount=float(next_step_cost), remaining=remaining,
                                   unit=mandate.unit, blocks=mission)
            journal.record(decide(STEP_GATES[step], question=question, at=stamp),
                           mission=mission)
            return self._write(
                mission, step, "asked",
                Message(OVER_CEILING.uk.format(amount=next_step_cost,
                                               unit=mandate.unit, remaining=remaining),
                        OVER_CEILING.en.format(amount=next_step_cost,
                                               unit=mandate.unit, remaining=remaining)),
                mandate=mandate, at=stamp)
        if money["over"] and money["question"] is not None:
            journal.record(decide(STEP_GATES[step], question=money["question"], at=stamp),
                           mission=mission)
            return self._write(mission, step, "asked", OVER_LIMIT, mandate=mandate,
                               detail=money["summary"], at=stamp)

        command_id = f"supervisor:{mission}:{step}:{state.version}"
        identifier = self._open(mission, step, mandate=mandate, actor=state.owner,
                                command_id=command_id, at=stamp)
        try:
            performed = (driver.plan if step == "plan" else driver.approve)(
                actor=state.owner, command_id=command_id)
        except SupervisorRefused as refused:
            # The driver saying "not this, and here is why" is an answer, not a
            # fault: it is recorded in the person's language and nothing retries.
            # The values that made the refusal specific travel with it, so the
            # ledger keeps the sentence a person would have read, not its template.
            summary = Message(refused.text("uk"), refused.text("en"))
            self._close(identifier, "refused", summary, None, _now())
            return Step(mission, step, "refused", summary, stamp, _now(),
                        mandate.granted_by, state.owner, None, identifier)
        except Exception as error:  # noqa: BLE001 - the reason is reported, not swallowed
            summary = Message(
                STEP_FAILED.uk.format(step=step, reason=str(error)[:300]),
                STEP_FAILED.en.format(step=step, reason=str(error)[:300]),
            )
            self._close(identifier, "waiting", summary, None, _now())
            return Step(mission, step, "waiting", summary, stamp, _now(),
                        mandate.granted_by, state.owner, None, identifier)
        # The gate is answered by policy only once the step it gates has happened,
        # so the journal never claims permission for something that did not run.
        journal.record(decide(STEP_GATES[step], context={"phase": performed.phase},
                              at=stamp), mission=mission)
        if performed.cost:
            costs.record(mission, amount=float(performed.cost), kind="reported",
                         task_key=command_id, stage_key=step)
        finished = _now()
        if performed.phase == state.phase:
            # A step that reports success while the mission stands still would put
            # a runner in a circle. Progress is the mission moving, not the call
            # returning.
            summary = Message(
                DID_NOT_MOVE.uk.format(step=step, phase=performed.phase),
                DID_NOT_MOVE.en.format(step=step, phase=performed.phase),
            )
            self._close(identifier, "waiting", summary, performed.detail, finished)
            return Step(mission, step, "waiting", summary, stamp, finished,
                        mandate.granted_by, state.owner, performed.detail, identifier)
        summary = PLANNED if step == "plan" else APPROVED
        self._close(identifier, "advanced", summary, performed.detail, finished)
        return Step(mission, step, "advanced", summary, stamp, finished,
                    mandate.granted_by, state.owner, performed.detail, identifier)

    def run(
        self,
        mission: str,
        driver: MissionDriver,
        *,
        passes: int = 8,
        next_step_cost: float = 0.0,
    ) -> tuple[Step, ...]:
        """Keep advancing while the mission keeps moving, and no further.

        The loop stops the moment a step does not move the mission — a question,
        a refusal, an empty queue — instead of spinning against the same wall.
        `passes` is a hard stop as well, so a driver that reports progress
        without making any cannot run forever.
        """
        taken: list[Step] = []
        for _ in range(max(1, int(passes))):
            step = self.advance(mission, driver, next_step_cost=next_step_cost)
            taken.append(step)
            if not step.moved:
                break
        return tuple(taken)

    # --------------------------------------------------------------- report

    def report(
        self,
        mission: str,
        *,
        language: str = DEFAULT_LANGUAGE,
        limit: int = 10,
    ) -> dict[str, Any]:
        chosen = normalise(language)
        mandate = self.mandate(mission)
        steps = self.history(mission, limit=limit)
        return {
            "mission": str(mission),
            "mandate": mandate.record(chosen) if mandate else None,
            "running_on_its_own": bool(mandate and mandate.live()),
            "summary": (mandate.record(chosen)["summary"] if mandate
                        else NO_MANDATE.text(chosen)),
            "steps": [step.record(chosen) for step in steps],
            "needs_person": [step.record(chosen) for step in steps if step.needs_person],
            "may": list(mandate.steps) if mandate else [],
        }


# --------------------------------------------------------------------- Core

class CoreMissionDriver:
    """Drives one real Core mission through planning and approval.

    Every call here is the same sequence the Temporal activity performs, run in
    this process because no worker is running in a local installation. The
    services do the deciding; this only puts them in order and hands each one
    the mission owner's name, which the mandate is what makes honest.
    """

    def __init__(self, storage: "SQLiteStorage", mission_id: int, *, invoker: Any = None,
                 capabilities: Mapping[str, Any] | None = None, workspace: Any = None):
        self.storage = storage
        self.mission_id = int(mission_id)
        self._invoker = invoker
        self._capabilities = capabilities
        self._workspace = workspace

    def _runtime(self) -> tuple[Any, Mapping[str, Any]]:
        if self._invoker is not None:
            return self._invoker, dict(self._capabilities or {})
        from .autonomous_planning_pipeline import RuntimePlanningInvoker
        from .runtime import AgentRuntime

        runtime = AgentRuntime(workspace=self._workspace)
        capabilities = {
            provider_id: provider.capabilities
            for provider_id, provider in runtime.providers.items()
        }
        return RuntimePlanningInvoker(runtime), capabilities

    def state(self) -> MissionState:
        from .autonomous_mission import AutonomousMissionService

        mission = AutonomousMissionService(self.storage).get(self.mission_id)
        return MissionState(mission.id, mission.mission_owner, str(mission.phase),
                            str(mission.disposition), mission.version, mission.mission_key)

    def plan(self, *, actor: str, command_id: str) -> Advanced:
        from .autonomous_authorization import AutonomousAuthorizationService
        from .autonomous_mission import AutonomousMissionService, MissionPhase
        from .autonomous_planning import AutonomousPlanningService
        from .autonomous_planning_pipeline import AutonomousPlanningPipelineService
        from .autonomous_proposal_verifier import AutonomousProposalVerificationService

        invoker, capabilities = self._runtime()
        missions = AutonomousMissionService(self.storage)
        planning = AutonomousPlanningService(self.storage, capabilities)
        authorizations = AutonomousAuthorizationService(self.storage, capabilities)
        pipeline = AutonomousPlanningPipelineService(self.storage, invoker, capabilities)
        verifier = AutonomousProposalVerificationService(self.storage)

        mission = missions.get(self.mission_id)
        manifest = planning.create_manifest(
            self.mission_id, proposal_key=command_id, actor=actor,
            command_id=command_id + ":manifest")
        role_models = {a.role_id: a.model for a in manifest.assignments}
        providers = tuple(sorted({a.provider_id for a in manifest.assignments}))
        authority = authorizations.grant_planning_authority(
            self.mission_id, planning_request_id=manifest.proposal_key,
            requested_action="ANALYZE", role_models=role_models, actor=actor,
            command_id=command_id + ":authority",
            reason="The studio is running this game under a mandate",
            provider_ids=providers)
        authorizations.assert_planning_authority(
            self.mission_id, authority.id, planning_request_id=manifest.proposal_key,
            requested_action="ANALYZE", role_models=role_models,
            provider_ids=providers, actor=actor)

        # DRAFT reaches BACKLOG_GENERATION through SPECIFICATION_ANALYSIS; a mission
        # already partway there only walks the phases it has left. Each transition
        # returns the mission at its new version, which is what the next one expects.
        wanted = [phase for phase in (MissionPhase.SPECIFICATION_ANALYSIS,
                                      MissionPhase.BACKLOG_GENERATION)
                  if _phase_index(str(phase)) > _phase_index(str(mission.phase))]
        for ordinal, target in enumerate(wanted):
            mission = missions.transition_phase(
                self.mission_id, target, actor=actor,
                command_id=f"{command_id}:phase:{ordinal}",
                expected_version=mission.version,
                reason="The studio is planning this game under a mandate")
        run = pipeline.execute(
            self.mission_id, manifest_id=manifest.id,
            planning_authorization_id=authority.id, actor=actor,
            command_id=command_id + ":pipeline")
        report = verifier.verify_and_present(
            run.id, actor=actor, command_id=command_id + ":verification",
            expected_mission_version=mission.version)
        # A plan that failed its own checks is not put up for approval. Saying so
        # is the whole point of running the checks.
        if str(report.status) != "READY":
            raise SupervisorRefused(PLAN_NOT_READY, count=len(report.findings))
        # A ready proposal is what moves the mission to waiting-for-approval, and
        # the verifier writes that itself. Repeating the transition here would be
        # a second claim about the same fact.
        return Advanced(str(missions.get(self.mission_id).phase),
                        verbatim(str(report.revision_digest)))

    def approve(self, *, actor: str, command_id: str) -> Advanced:
        raise SupervisorRefused(CANNOT_APPROVE_YET)


_PHASE_ORDER = ("DRAFT", "SPECIFICATION_ANALYSIS", "BACKLOG_GENERATION",
                "WAITING_FOR_BACKLOG_APPROVAL")


def _phase_index(phase: str) -> int:
    try:
        return _PHASE_ORDER.index(str(phase))
    except ValueError:
        return len(_PHASE_ORDER)
