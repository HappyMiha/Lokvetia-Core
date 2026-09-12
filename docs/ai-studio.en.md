<a id="постановка-задачі-режим-ai-студія--від-ідеї-до-гри-без-ручних-воріт"></a>
# Statement of work: "AI studio" mode — from an idea to a game with no manual gates

<!-- translation-metadata:start -->
<details>
<summary>Translation source and currency</summary>

Translation source: [ai-studio.uk.md](ai-studio.uk.md). Source SHA-256 (UTF-8/LF): `7241ef02b5dca03e0cc0289643ebba30f37eac381c400bff39aa31e7a03e2c03`.

Currency checks: [Core](https://github.com/HappyMiha/Lokvetia-Core/actions/workflows/planning.yml?query=branch%3Amain) · [Lokiravia](https://github.com/HappyMiha/Lokiravia/actions/workflows/planning.yml?query=branch%3Amain). English is a documentation translation; canonical requirements and evidence statuses are unchanged.

</details>
<!-- translation-metadata:end -->

Українська: [original](ai-studio.uk.md).

**Project:** Lokvetia Core (with Lokiravia as the storefront and account area)
**Status:** accepted as the direction of development; every epic is broken into tasks in [game-creator-backlog.json](../examples/game-creator-backlog.json) under the `AF-ST` prefix.
**Date:** 12 September 2026

> An entry in the backlog is not an implementation. Every `AF-ST` item is
> `status:proposed`, and none will be declared done without evidence and the
> owner's acceptance.

---

<a id="1-навіщо"></a>
## 1. Why

Today the code is built as a tool for a single engineer-operator: a person describes an idea, and then the system requires them to approve a backlog revision, an authorisation and an environment profile by hand before anything runs. For an engineer that is right. For a person who wants a game it is a wall: they have no idea what a "backlog revision" is or why it must be approved.

There are also two practical gaps:

- Core cannot be **downloaded and installed locally** anywhere — even though the whole codebase (hardware scanning, a local HTTP boundary, engine adapters) assumes exactly a local run.
- The demo at `test.lokvetia.com` reports the characteristics of **the container**, not of the user's machine, because the process lives in a container — and that reads as a bug, although it follows from the deployment model.

**Goal:** the user describes a game, presses one button, and a studio of agents starts developing it. The person watches the backlog and the statuses, plays the intermediate builds, stops the work when they want to, comments, and starts the next cycle.

<a id="2-цільовий-сценарій-базовий-користувач"></a>
## 2. The target scenario (a basic user)

1. Installs Core locally (an installer for Windows, macOS and Linux).
2. Goes once through the **AI provider wizard**: what they already have (subscriptions), what can be raised locally (if the GPU allows it), what can be arranged through the platform (Claude, Codex and others). The result is a set of available performers for the roles.
3. Describes the game in their own words. Presses **"Create the game"**.
4. The studio asks **a few short clarifying questions** — 2D or 3D, Godot or Unity, style, platform — each with a sensible default and an "up to you" button. It may offer material to read. This is not a gate: with no answer the default is taken and the work begins.
5. The agents draw up a plan and **start immediately**. No "approve the revision".
6. The user sees **the whole backlog** (not only the current task) with statuses: `planned` · `in progress` · `done` · `blocked`, grouped by stage.
7. At stage boundaries marked in advance a **test slice** appears — "you can play this": run it in the browser, or download a build.
8. At any moment: **Pause** → write remarks, corrections, wishes → **Continue**. The studio replans the rest of the backlog with the comments in mind and carries on.

<a id="приклад-ритму-етапів"></a>
### An example rhythm of stages

| Stage | Tasks | What is at the end |
|---|---|---|
| Preparing the platform and the tools | ~10 | nothing playable, only readiness |
| The base of the game (loop, input, scene, saving) | ~25 | **Test 1: the first level** |
| Filling in the first level | ~7 | **Test 2** |
| Mechanics and content | ~17 | **Test 3** |

The counts are an example; the studio derives them from the plan, but **the rule does not change: every stage ends either with a playable slice or with an explicit "there is nothing to test here yet"**.

<a id="3-студія-агентів"></a>
## 3. The studio of agents

Every role is a separate agent with its own contract (input, output, tools, evidence). The code already has `roles.py` / `RoleRegistry` and five planning roles (`mission_analyst`, `product_requirements_analyst`, `software_architect`, `backlog_planner`, `backlog_reviewer`) plus `Environment Bootstrap` and `Developer`.

<a id="31-мінімальний-склад-за-замовчуванням--дві-ролі"></a>
### 3.1. The default minimum is two roles

A studio of seven roles on a long backlog costs money and needs several subscriptions. So **the default is two roles working in sequence**, so that one subscription is enough:

- **The planner** — takes the idea apart, makes the design decisions, cuts the work into tasks and stages, and holds the slice boundaries;
- **The developer** — carries out the tasks in the engine, builds, and prepares the playable slice.

Execution is **sequential**: at any moment one agent is working, which means one stream of requests to a model. That is a predictable bill and no race for the rate limit.

<a id="32-підсилення-команди-на-вимогу"></a>
### 3.2. Reinforcing the team on demand

The user adds a role from the catalogue at any time — typically when they see a problem: many defects → they add a **tester**; they want it to look better → **UI/UX** or an **artist**; they want parallelism → a second developer.

The catalogue of roles (available but **disabled** by default): producer/tech lead, game designer, UI/UX, artist/assets, build engineer, QA, backend, analyst, marketing director, sound designer, localisation.

Adding a role shows the consequence before it is confirmed: the approximate increase in cost and whether another subscription is needed (parallel roles mean parallel streams to the models).

<a id="33-правила"></a>
### 3.3. Rules

- roles are added and disabled without code — through the catalogue;
- **each role can be assigned its own model and provider** (for example, the planner on Opus, the developer on Codex, the tester on a local model);
- a role with no model assigned takes the profile default;
- `incompatible_duties` come into force **as soon as there are enough roles**: once there is a separate tester, the developer no longer accepts their own work. In the minimum pair, acceptance rests on the engine's evidence (see §6).

<a id="4-де-що-виконується-гібрид"></a>
## 4. Where things run (hybrid)

- **By default, cloud build workers.** The user installs nothing for a build; Godot and the toolchain live on a worker, not in the web container.
- **Your own PC as a worker** — for advanced users: Unity with a licence, a GPU, large assets. A local Core registers the machine as a worker.
- **Local AI workers are in v1.** If the hardware allows it, the model for a role is raised on the user's machine, and that role works with no subscription at all. Core checks the hardware and says plainly what it will carry and what it will not. A role on a local model is equal to a cloud one: the same "role → provider" assignment.
- The site's web container is **never a place to build**. Hardware scanning and engine adapters run only on a worker, and the report says explicitly: "machine: cloud worker X" / "your PC".
- The foundation exists: `worker_capacity_pools`, leases and admission in `worker_admission.py`. What is needed is moving the adapters (`godot_engine`, `unity_setup`, `hardware_inventory`) beyond the worker boundary.

<a id="5-провайдери-ai-та-ключі"></a>
## 5. AI providers and keys

A step after installing a local Core (and available later in the settings):

1. **My subscriptions** — the user states which they already have (Claude, Codex, others) and connects a key or an account.
2. **Local models** — Core checks the hardware and offers to raise locally what the GPU will carry; if it will not, it says so plainly, without "give it a try".
3. **Through the platform** — arranging a subscription without leaving the product.
4. The result is a "role → provider → model" table with a default profile for the basic user.

**The rule for starting: without at least one source of execution, development does not begin.** A source is a connected subscription of one's own, a subscription arranged through the platform (possibly with a trial limit), or a local model that passes the hardware check. Until then the "Create the game" button is inactive and explains what is missing.

Keys are stored locally in Core (`credentials.py`, `credential_connections.py` exist), never travel to somebody else's worker in the raw, and every expense is written to the journal bound to a role and a task.

<a id="6-що-стається-з-поточними-підтвердженнями"></a>
## 6. What happens to the present "approvals"

The checks do not disappear — **the manual clicking** does. Each present gate moves into one of three states:

| The gate today | Becomes |
|---|---|
| Approving a backlog revision | automatic, by policy; the backlog is visible and can be edited while paused |
| Authorising execution | automatic when the mission starts, with a journal entry |
| Approving the environment profile | automatic; a failed check is a "fix the environment" task, not a stop screen |
| Checking database schema compatibility | stays automatic (it protects data) |
| Evidence from the engine (`playable_versions`) | stays: a slice without the engine actually running is not marked playable |

**A person is asked only when:** money would go over the limit, an action cannot be undone (publishing, deleting), or a request goes beyond the declared level of capability (`capability_levels.py` — `scoped_prototype` / `investigation` / `unsupported`). Then it is one short question in the feed, not a block on the pipeline.

<a id="7-функціональні-вимоги"></a>
## 7. Functional requirements

**F1. One button.** From the text of an idea to work starting — with no mandatory approval of any kind.

**F2. Clarifications with defaults.** No more than 5 questions at a time, each with a default and an "you decide" option. If five really are not enough for the planner, it does not silently keep asking: it puts one decision to the user — **"keep clarifying, or start development?"** — and by the answer either opens another block of questions (also up to 5) or starts with what it has, writing the assumptions into the plan. Answers and assumptions are fixed in the plan and visible on the progress screen.

**F3. The whole backlog, for the user.** Every task, grouped by stage; the statuses `planned/in progress/done/blocked`; who (which role) leads a task; names in human language, with no internal codes.

**F4. Test slices.** Every stage has a defined boundary with a "can be tested" mark. A slice is a record in `playable_versions` with an artifact, a checksum and evidence of a run; in the interface, a "Play" / "Download" button.

**F5. Pause and the edit cycle.** Pause stops the issuing of new tasks (the current ones finish properly). The user leaves comments on particular tasks or on the game as a whole. "Continue" → the rest of the backlog is replanned with the comments in mind → a new iteration. The history of the cycles is kept.

**F6. Studio settings.** A basic user sees nothing superfluous. An advanced one: add or remove a role, assign a role its model, set cost limits, connect their own worker.

**F9. The paid-software rule.** If a task needs a paid tool (Unity, paid assets, a paid service), the studio does not stop in a dead end but gives the user a choice: connect their own subscription and log in themselves, buy through the platform, take a suggested free alternative — or decline. **Declining does not break the mission: the planner rebuilds the backlog under the constraint** and marks plainly what was cut because of it.

**F7. A Core installer.** Download from the site, install, update (`application_update.py` already exists), first run → the provider wizard.

**F8. Honest reports.** Any report about hardware, environment or a build is signed by the machine it ran on. Container data is never presented as data about the user's PC.

<a id="8-нефункціональні-вимоги"></a>
## 8. Non-functional requirements

- **Transparent cost:** the current spend on a mission, the forecast to the end of the stage, a limit that stops the work.
- **Isolation:** one user's work does not see another's; a worker never receives raw keys.
- **Recoverability:** a worker or Core crashing does not lose progress; the task returns to the queue.
- **Observability:** for every task — who did it, with which model, what it cost, what evidence there is.
- **UI latency:** statuses refresh at least once every 5 seconds.

<a id="9-обсяг-v1--поза-обсягом"></a>
## 9. The scope of v1 / out of scope

**In v1:** Godot 2D, the minimum pair of roles plus the catalogue for reinforcement, cloud workers, **local AI workers**, the provider wizard, the backlog with statuses and slices, pause and the edit cycle, the paid-software rule, a cost limit and forecast, an installer.

**Out of v1:** Unity (detection and instructions only, as today), 3D, multiplayer, consoles, VR, an asset store, publishing to a store, several people working together on one game.

<a id="10-критерії-приймання"></a>
## 10. Acceptance criteria

1. A person with no technical background, from a clean machine: installed Core → connected a provider → described a game → pressed the button → **made no "approve" of any kind** → saw the first playable slice N minutes later.
2. The backlog shows every task with its stage; each stage ends either with a slice or with an explicit "nothing to test" mark.
3. Pause → the comment "I want a double jump" → continue → new tasks for it appeared in the backlog, and the next slice contains them.
4. The hardware report shows the machine the build really runs on, and says so plainly.
5. An advanced user assigned three roles three different models — and the task journal shows that those are the ones that worked.
6. Nowhere does the interface ask to "approve the backlog revision" in the words of the internal model.
7. A game starts with two roles and one connected subscription; the bill for the mission is predictable, because the agents work in sequence.
8. The user added a tester mid-development — new checking tasks appeared in the backlog, and the old work was not lost.
9. The planner ran out of its five questions — the user got the decision "keep clarifying or start", not a silent stream of questions.
10. A task hit paid software — the user declined — the backlog was rebuilt, and it shows plainly what was cut.
11. A role on a local model completed a mission with no subscription at all, on a machine Core found suitable.

<a id="11-рішення-та-відкриті-питання"></a>
## 11. Decisions and open questions

**Decided:**

- **The quality threshold.** It stays as it is for now: a slice is marked playable only after the engine has actually run. Further options — an estimate of the probability of success, a configurable threshold, a separate reviewer — are the next iteration, after this mode has been tried in practice.
- **Cost.** The default is two roles in sequence, that is, one subscription. Reinforcing the team is a deliberate step by the user with the consequence shown. The limit and the forecast are in v1.
- **Paying for execution.** Without at least one source (one's own subscription, one bought through the platform, or a local model) development does not start.
- **Paid software.** Own subscription → buy → alternative → decline with the backlog rebuilt (F9).
- **The size of a stage.** The planner decides, with no hard ceiling; the one requirement stays: a stage boundary must end either with a playable slice or with an honest "nothing to test" mark.

**Still open:**

- **Charging for cloud build workers** — included in the platform tariff or billed separately (building locally on the user's machine is free either way).
- **The trial limit** — how much exactly the platform gives a new user before their first subscription.
- **Rights to assets.** `asset_provenance.py` already exists; what to do when a model generated an asset with no clean provenance has to be decided.
- **Parallelism.** Once there are more roles, queue rules and a limit on concurrent streams per subscription are needed, so as not to run into the provider's rate limit.

<a id="12-розбивка-на-епіки"></a>
## 12. The breakdown into epics

- **E1. The installer and the first run** — the distribution, updates, the AI provider wizard.
- **E2. The studio role catalogue** — extending `RoleRegistry`, the minimum pair of roles, reinforcement on demand with the consequence shown, assigning a model to a role.
- **E3. A pipeline with no gates** — automatic approval by policy, the journal, the short list of exceptions that really need a person.
- **E4. A backlog for a person** — human names, grouping by stage, statuses, progress.
- **E5. Test slices** — stage boundaries, playable builds, "Play"/"Download" on top of `playable_versions`.
- **E6. Pause and the edit cycle** — stopping, comments, replanning, the history of cycles.
- **E7. Workers** — moving the build and the hardware scan beyond the web container, registering one's own PC, local AI workers with a hardware check.
- **E9. The paid-software rule** — the choice "own subscription / buy / alternative / decline" and rebuilding the backlog under the constraint.
- **E8. Cost and limits** — accounting per task and role, the forecast, stopping at the limit.

---

<a id="13-задачі-в-беклозі"></a>
## 13. The tasks in the backlog

The machine-readable source is [examples/game-creator-backlog.json](../examples/game-creator-backlog.json),
schema v2, milestone `m4`, product `ai-studio`. The mode is accepted by `AF-ST-010`,
which is also the M4 release gate.

| ID | Parent | Title | About |
|---|---|---|---|
| **AF-ST-E1** | — | **The installer and the first run** | Core can be downloaded, installed and updated; the first run leads into the AI provider wizard. |
| **AF-ST-E2** | — | **The studio role catalogue** | The default is two roles in sequence; every other role is enabled deliberately, with its consequence shown and its own model. |
| **AF-ST-E3** | — | **A pipeline with no manual gates** | The checks stay, the clicking goes; a person is asked only about money, the irreversible, and what is beyond the declared capability. |
| **AF-ST-E4** | — | **A backlog in human language** | The user sees the whole plan by stage and status, with no internal codes. |
| **AF-ST-E5** | — | **Test slices at stage boundaries** | Every stage ends either with a playable slice or with an honest "nothing to test" mark. |
| **AF-ST-E6** | — | **Pause and the edit cycle** | Pause, comments, replanning the rest of the backlog, and the history of cycles. |
| **AF-ST-E7** | — | **Workers and honest reports** | Builds and hardware scans run on a worker and are signed by the machine; one's own PC and local AI workers are equal citizens. |
| **AF-ST-E8** | — | **Cost, forecast and limits** | Spend per task and role, a forecast to the end of the stage, and a stop at the limit. |
| **AF-ST-E9** | — | **The paid-software rule** | A paid tool does not lead the mission into a dead end: a choice, and on a decline a rebuilt backlog that shows what was cut. |
| AF-ST-101 | AF-ST-E1 | Build the Core distribution and the first run | The user downloads Core from the site, installs it and lands straight on the first run. |
| AF-ST-102 | AF-ST-E1 | Walk through the AI provider wizard | Once after installation: what already exists, what will run locally, what can be arranged through the platform. |
| AF-ST-201 | AF-ST-E2 | A role catalogue with a minimum of two | Planner and developer work in sequence by default; every other role is in the catalogue, disabled. |
| AF-ST-202 | AF-ST-E2 | Assign a model to a role and show the consequence of reinforcing | Every role may have its own provider and model; adding a role shows the increase in cost before it is confirmed. |
| AF-ST-301 | AF-ST-E3 | Replace the manual gates with policy and a journal | Approving the backlog revision, authorising execution and the environment profile happen automatically, by policy. |
| AF-ST-302 | AF-ST-E3 | Reduce the questions for a person to three cases | A person is asked only about spending beyond the limit, an irreversible action, or a request beyond the declared capability. |
| AF-ST-401 | AF-ST-E4 | Show the whole backlog by stage, in human language | The user sees every task grouped by stage, with its status and the role leading it. |
| AF-ST-501 | AF-ST-E5 | End a stage with a slice or an honest mark | A stage boundary yields a playable version or says plainly that there is nothing to test yet. |
| AF-ST-601 | AF-ST-E6 | Pause, comments and replanning the rest of the backlog | Pause stops the issuing of new tasks, comments become new tasks, and the history of cycles is kept. |
| AF-ST-701 | AF-ST-E7 | Move builds and hardware scans beyond the web container | Engine adapters and hardware scanning run on a worker, and the report is signed by the machine. |
| AF-ST-702 | AF-ST-E7 | Register one's own PC as a worker | An advanced user offers their machine for builds with a licensed Unity, a GPU or large assets. |
| AF-ST-703 | AF-ST-E7 | Give a role a local model with no subscription | If the hardware allows it, the model for a role is raised on the user's machine and works as an equal to a cloud one. |
| AF-ST-801 | AF-ST-E8 | Count cost per task and role, with a forecast and a limit | The current spend, the forecast to the end of the stage and the stop at the limit, understandable without engineering knowledge. |
| AF-ST-901 | AF-ST-E9 | Offer a choice where paid software is needed | Own subscription, buying through the platform, a free alternative, or declining with the backlog rebuilt. |
| AF-ST-010 | — | Accept the AI-studio mode on a clean machine | A person with no technical background goes from installation to the first playable slice with no "approve" of any kind. |

<a id="14-на-що-це-лягає-в-коді"></a>
## 14. What this lands on in the code

The statement does not start from nothing. What already exists and what will have to change:

| Needed | Where it already is | What is missing |
|---|---|---|
| Roles and the registry | `roles.py`, `workforce.py` | a catalogue with disabled roles, the minimum pair, a model per role |
| Policy and authorisation | `policy.py`, `autonomous_authorization.py`, `autonomous_backlog_approval.py` | an automatic decision by policy instead of a manual click, the journal of decisions in the interface |
| Capability levels | `capability_levels.py` | reducing the questions for a person to three cases and showing them in the feed |
| The backlog | `backlog.py`, `backlog_revisions.py` | human names, grouping by stage, statuses for the user |
| Playable slices | `playable_versions.py`, `local_games.py` | the stage boundary as an explicit mark, "Play"/"Download" in the interface |
| Pause and edits | `game_feedback.py`, `work_status.py` | replanning the rest of the backlog from the comments, and the history of cycles |
| Workers | `worker_admission.py`, `worker_runtime.py` | moving the adapters beyond the web container, registering one's own PC, signing reports with the machine |
| Local models | `local_model_scheduler.py`, `environment_model_probe.py` | the hardware check before offering, and equality with a cloud role |
| Cost | `work_status_store.py`, `execution_telemetry.py` | accounting per role, the forecast to the end of the stage, stopping at the limit |
| Installing and updating | `application_update.py`, `installation_publication.py` | a distribution for three operating systems and a first run with the provider wizard |
| Paid software | `connector_eligibility.py` | the choice of four options and rebuilding the backlog on a decline |
