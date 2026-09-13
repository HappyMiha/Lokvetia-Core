# One screen for the person whose game it is

Everything the AI-studio modules know is reachable over the API and the command
line. This is the page a person actually uses: `/studio`, in Ukrainian or
English.

Requirement trace: the interface halves of `AF-ST-401`, `AF-ST-601`,
`AF-ST-801`, `AF-ST-901`, `AF-ST-201`, `AF-ST-701` and `AF-ST-301`. None is labelled
accepted here.

## What is on it

- **The plan of work**, whole and by stage, with each task's state in four
  words, the role leading it where one is recorded, and one line for what
  happens next. A stage that produced a playable slice offers Play; a stage
  that did not says out loud that there is nothing to test yet.
- **Who is driving the work** — whether anyone has asked the studio to run this
  game on its own, what that mandate allows, until when, up to how much, and
  every step the studio has taken here. Handing it the keys takes two clicks on
  purpose: the first spells out that it will then act in your name, the second
  does it. Taking them back leaves everything it already did on the screen.
- **Yours to decide** — every question waiting on a person in one place: an
  autonomy question, a paid tool with its ways out, a spending limit reached.
  When there is nothing, it says so rather than showing an empty box.
- **Money** — spent, reserved, the limit, the per-role table, and the forecast.
  Where the forecast is not earned, the page prints the reason instead of a
  number.
- **Who is working** — the roster with each role's duty, which are off, which
  model a role carries, and the sentence about who accepts the work. Turning a
  role on takes two clicks on purpose: the first shows what it would add and
  whether it needs another subscription, and only the second switches it on.
  The pending offer survives a refresh of the list, so a background reload
  cannot throw away what the person is in the middle of reading.
- **Pause and edits** — the cycle and its state, a name, a comment box, and
  Pause / Send / Continue. Nothing happens without a name, because every one of
  these is recorded against a person.
- **Where this runs** — every machine by its own signature, with the ones that
  do not build marked as such.

## What the page refuses to do

- **No internal codes.** A browser test renders the whole page and asserts that
  no `AF-…` identifier appears anywhere in it.
- **No Play button without a build.** The button exists only where a stage
  boundary names a version that is really in the playable ledger.
- **No invented forecast.** The screen repeats the module's own sentence when
  there is not enough measured work to forecast from.
- **No silent failure.** A game with no plan yet is a normal state: the rest of
  the screen still loads, and the summary says what is missing — including that
  development will not start at all until a source of execution is checked.

## How it is verified

A real browser against a seeded mission: the plan with its four states, the
open questions, the money, the roster sentence, the pause and the comment, the
machine signatures, and the refusal to continue without a name. The
accessibility suite covers `/studio` in both languages at 320 CSS px and at
laptop width, alongside the other pages.

## What is not claimed

- **The page reads and asks; it does not plan.** Continuing hands the comments
  to whoever replans, and says so.
- **A model is still assigned from the command line.** The screen turns roles
  on and off; giving one role its own provider and model is `lokvetia studio
  role-model`.
- **The Play link points at the local games page** with the version digest; the
  page that runs a specific slice is separate work.
- **Creating a game does not hand the studio the keys.** The intake writes the
  mission; the mandate is a separate, named act on this screen, because granting
  one on a person's behalf is exactly what a mandate exists to prevent. See
  [The studio starts the work itself](studio-supervisor.md).
