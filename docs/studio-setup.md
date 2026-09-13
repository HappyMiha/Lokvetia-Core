# Who is in the studio, which machine answers, and when work can start

The [implementation audit](ai-studio-audit.en.md) adds a concrete local worker
setup command. Unlike merely recording a source, local studio launch checks
real API/CLI qualification and the exact model and provider-profile digests.

Requirement trace: `AF-ST-201` and `AF-ST-202` (epic `AF-ST-E2`), `AF-ST-701`,
`AF-ST-702` and `AF-ST-703` (epic `AF-ST-E7`), and `AF-ST-102` (epic
`AF-ST-E1`). None is labelled accepted here; what is missing is at the bottom.

## Two roles, and a consequence before a third

A new game starts with a planner and a developer, working one after another:
one stream of requests to a model, a predictable bill, no race for a rate
limit. Every other role — tester, UI/UX, artist, build engineer, sound,
localisation, analyst — is in the catalogue, switched off.

Turning one on is a decision, and the consequence is readable before the
switch: roughly what it adds, and whether it needs another subscription
(because roles that run at the same time are separate streams to a provider).
Reading a consequence changes nothing — a test asserts that too.

The added cost is an **estimate with its basis**, from measured tasks of this
mission. With fewer than three, it says the number is not known rather than
inventing one.

**Acceptance follows the roster, not a setting.** While the developer is the
only one who could look at their own work, acceptance rests on the engine's
evidence. The moment a tester is on, the developer no longer accepts their own
work — derived from who is enabled, and reported in words.

Each role can have its own provider and model; a role with none uses the
profile default, and says so. Assigning a model does not turn a role on.

```bash
lokvetia studio team       --mission cat-coins --language en
lokvetia studio add-role   --mission cat-coins --role tester --actor miha
lokvetia studio role-model --mission cat-coins --role planner --provider claude --model opus --actor miha
```

## Which machine answers

The demo showing a container's CPU as if it were the user's PC is not a
cosmetic bug: it is a report naming the wrong machine. So every report carries
the machine it came from, and there is no way to produce one without naming it —
`your PC: desktop-tefqhlo`, `cloud worker: build-worker-3`.

- **The web container never builds or scans hardware.** Asking it is refused
  with the reason, rather than answered with the container's own numbers.
- **A machine only gets work it can do.** It declares what it has — an engine, a
  licence, a GPU — and a task needing something else is refused with the missing
  thing named.
- **A worker never receives raw keys.** A machine record has nowhere to put one;
  a test asserts the table has no such column.

```bash
lokvetia studio register-machine --machine pc --name desktop --kind this_pc --can godot --can unity --video-memory-gb 8
lokvetia studio machines --language en
```

## Nothing starts without somebody to do the work

Three ways to have a worker: a subscription you already have, one arranged
through the platform, or a local model. Until at least one is **checked**, the
"Create the game" button is inactive and says which is missing — a key that was
typed in but never used is `unverified` and unlocks nothing.

A local model is offered only after the hardware has been looked at. When the
card will not carry it, the wizard says so with the numbers instead of "give it
a try".

```bash
lokvetia studio first-run --language en          # exit 3 while nothing can start
lokvetia studio connect --source claude --kind own_subscription --name Claude --state verified
lokvetia studio connect --source llama --kind local_model --name "Llama 8B" --machine pc --needed-gb 10
```

Over HTTP: `GET /api/studio/roster/{mission}`,
`/api/studio/roster/{mission}/consequence/{role}`, `/api/studio/machines` and
`/api/studio/first-run` to read; `POST` to the roster, machine and source paths
to change, behind the usual confirmation header.

## What is not claimed

- **No installer is built here.** `AF-ST-101` — a distribution for Windows,
  macOS and Linux, with checksums and an update path — is release engineering
  that this work does not do. What exists is the first run the installer would
  land on.
- **Nothing routes work to a machine yet.** Admission answers "may this machine
  do this", and reports carry their signature; moving the engine adapters
  themselves behind that boundary is the rest of `AF-ST-701`.
- **The wizard stores no keys.** Connecting records that a source exists and
  whether it was checked; the key material belongs to the credential store.
- **A local model is judged on video memory alone.** That is the one number this
  check has; it is not a promise about speed.
