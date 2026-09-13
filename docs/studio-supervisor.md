# The studio starts the work itself

For the current local launch path and remaining gaps, see the
[implementation audit](ai-studio-audit.en.md). `/api/studio/create` now records
the authenticated owner's planning mandate and queues work through a local
runner. The older mandate-only endpoint still records authority without dispatch.

Everything else in this product decides *whether* something may happen and
records that it did. Nothing moved a mission. A person described a game, the
intake wrote a real mission in `DRAFT`, and there it stayed — because no
supervisor, daemon or workflow in the shipped application ever picked one up.

The parts were all there and all real: the planning pipeline, the gates, the
money, the provider workers, the Godot build. What was missing was one wire,
from *a person pressed Create* to *the work began*. This is that wire.

Requirement trace: the missing half of `AF-ST-301` — "from the text of an idea
to the start of work there is no mandatory human approval". The earlier work
under that task delivered the policy and the journal; this delivers the thing
that acts on them. Not labelled accepted here.

## Nothing runs without a mandate

Every mission-level act in Core demands the mission owner's name. A supervisor
that simply signed that name would forge the owner's consent on every automatic
step, which is worse than asking.

So a named person records, once, what the studio may do on this game without
being asked again: **which steps, up to how much money, until when**. Each
automatic step then carries both — the owner's name, because the work is theirs,
and the mandate that made signing it honest.

- A mandate is superseded, never edited. Granting a new one closes the old one
  and keeps it; a database trigger refuses any other change to a mandate, which
  may only ever gain its revocation.
- A mandate runs out. There is no permanent one, and a term of zero is refused.
- Revoking stops the studio starting anything else. It does not erase, undo or
  hide what was already done.
- With no live mandate the supervisor does nothing at all and says so. It never
  guesses what a person would have allowed.

On the screen this takes two clicks on purpose: the first spells out that the
studio will act in your name, the second hands it the keys.

## One step per call

`advance()` performs at most one thing and returns what it did or why it did
not. `run()` is a loop over it that stops the moment the mission stops moving.
The consequences are worth stating: it can be stopped between steps, it is the
same code in a test as in production, and a crash loses at most one step.

**Progress means the mission moved.** A step whose driver reports success while
the phase stays where it was is recorded as `waiting`, not as progress, and the
loop stops. A driver cannot talk the studio into a circle.

## What stops it, in the order a person would ask

1. **Nobody delegated this.** No mandate, nothing happens.
2. **The mandate ran out**, and the record says when.
3. **There is no checked source of execution.** Nothing can start; an
   *unverified* subscription unlocks nothing.
4. **Somebody is already being asked something** that blocks this game.
5. **The money.** Two separate promises are checked, and the smaller wins: the
   spending limit on the game, and the ceiling in the mandate. Going past either
   raises the over-budget question in the feed instead of spending.
6. **The mission is not at a step this studio drives**, named plainly.
7. **The mandate does not cover the step** the mission needs.

Each of these is a sentence in both languages, written into the ledger with the
mandate and the person it belongs to.

## A step in flight is never repeated

Interrupted work may already have run and may already have been paid for.
Repeating it to make sure is how a person is charged twice. On the next call any
step left in flight becomes `unknown`, says that it may have happened and may
have cost money, and waits for a person. Nothing relaunches.

## The ledger is a history of changes

A runner asking every half minute why it cannot start would otherwise write the
same sentence forever, so an answer identical to the last one returns the
existing record rather than adding another. A finished step is never rewritten;
a trigger enforces it.

## What one step actually does

`plan` runs, in this process, the same sequence the Temporal activity performs —
because in a local installation no worker is running:

1. bind the planning models for this mission;
2. grant the planning authority, then revalidate it;
3. walk the phases the mission has left, `DRAFT` → `SPECIFICATION_ANALYSIS` →
   `BACKLOG_GENERATION`;
4. run the planning pipeline across its roles;
5. verify the proposal and present it.

A verified, ready proposal is what moves the mission to
`WAITING_FOR_BACKLOG_APPROVAL`, and the verifier writes that itself. A proposal
that failed its own checks is **not** put up for approval: the studio says how
many findings there were and stops. Saying so is the whole point of the checks.

The gate is answered in the autonomy journal only *after* the step has happened,
so the journal never records permission for something that did not run.

## How it is verified

A real mission created through the intake, planned by the pipeline's own golden
fixtures, reaching a real backlog revision and
`WAITING_FOR_BACKLOG_APPROVAL` — with nobody pressing anything. Alongside it,
every refusal above, both immutability triggers, the restart behaviour, the
money ceilings, and a browser test of the card on `/studio`.

## What is not claimed

- **The studio does not approve a plan on its own.** Approving binds the work to
  one commit in a checked-out repository and to an executor that does not exist
  in this product yet. The step exists in the vocabulary and the driver refuses
  it in the person's language, rather than pretending.
- **Nothing executes the approved plan.** After approval there is a backlog and
  an execution epoch, and no code in this repository writes the game.
- **The supervisor is not a durable execution service.** The local studio now
  queues explicitly requested planning in a background thread. It does not
  automatically resume arbitrary missions or execute approved game work.
- **A mandate is not authentication.** It records a name the caller supplied,
  like every other named act in the studio.

## Commands

```
lokvetia studio mandate --mission <game> --actor <name> --allow plan --ceiling 20
lokvetia studio run --mission <game> --mission-id <id> [--once]
lokvetia studio steps --mission <game>
lokvetia studio revoke-mandate --mission <game> --actor <name>
```

`run` exits `3` when the studio cannot go further without a person — the same
convention the rest of the studio commands use.
