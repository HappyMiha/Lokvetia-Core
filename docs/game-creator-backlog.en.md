<a id="беклог-lokvetia-core-від-ідеї-до-власної-гри-12"></a>
# Lokvetia Core backlog: from an idea to your own game, ages 12+


<!-- translation-metadata:start -->
<details>
<summary>Translation source and currency</summary>

Translation source: [game-creator-backlog.uk.md](game-creator-backlog.uk.md). Source SHA-256 (UTF-8/LF): `45502fb8ace32dd907938f0bd132153e20a123f5942bdba5a3f06f1189f9f74d`.

Currency checks: [Core](https://github.com/HappyMiha/Lokvetia-Core/actions/workflows/planning.yml?query=branch%3Amain) · [Lokiravia](https://github.com/HappyMiha/Lokiravia/actions/workflows/planning.yml?query=branch%3Amain). English is a documentation translation; canonical requirements and evidence statuses are unchanged.

</details>
<!-- translation-metadata:end -->

Українська: [original](game-creator-backlog.uk.md).

Date: **5 September 2026**. This is the active product plan; the [current-state audit](product-audit-2026-09-05.md) explains its rationale. The machine-readable source is [game-creator-backlog.json](../examples/game-creator-backlog.json), schema v2. All new tasks are **proposed**; none is declared implemented.

<a id="наступний-напрям-режим-ai-студія"></a>
## Next direction: the "AI studio" mode

The ["AI studio"](ai-studio.en.md) statement is accepted as the direction of this
same plan. Its epics and tasks live in the same manifest with the `AF-ST` prefix,
milestone `m4`, and are accepted by task `AF-ST-010`. They cancel no `AF-GC` task:
they remove the manual gates from work those tasks have already proved.

<a id="мета-й-межі"></a>
## Goal and boundaries

Creators describe a game, identify their available cloud/local AI, follow understandable connection steps and receive a plan suited to their PC. The system installs approved components, prepares the environment and develops the game in small, verified steps. Creators periodically select “Play”, give feedback and can restore an earlier version.

“Almost any complexity” is a development direction, not a first-release criterion. The first proof is **Windows, Godot, GDScript, a small 2D game**, a qualified cloud worker and an independent reviewer. Local-only/hybrid, Unity, 3D and larger projects follow. Other operating systems and engines become supported only after qualification; this does not mean they will remain unavailable forever.

For a 12-year-old creator, external account flows must comply with the particular provider's rules. An adult configures access/spending where required; local/offline remains a separate M2 outcome. We do not promise that parental permission automatically permits use of any third-party service.

The core remains project-neutral: game templates, engine commands and acceptance contracts live in game packs. Existing policy, audit, worktree, memory and recovery mechanisms are reused.

<a id="шлях-користувача"></a>
## User journey

1. **My idea:** description → brief clarification → controls, objective and first playable.
2. **My AI:** available products → official login/key → verification of the actual model and capabilities.
3. **My PC:** read-only inventory → recommendation → exact installation plan, disk space, costs and required personal actions.
4. **Creation:** automatic preparation → real changes → engine checks → independent review → working version.
5. **Play and change:** Play a specific build → feedback → next version; Pause, Stop and recovery remain available.

Technical IDs, leases, JSON, routing and logs are available in the details. The main screen shows the outcome, progress and one next action.

<a id="черговість-та-приймання"></a>
## Sequence and acceptance

| Stage | Outcome | Gate |
|---|---|---|
| M0 | Truthful statuses, working basic interactions, compatible live roles, reproducible CI | All M0 tasks accepted |
| M1 | First Godot game, Play → feedback → v2 on a clean PC | AF-GC-026, including AF-GC-040 research |
| M2 | Local-only and hybrid, accounting for RAM/VRAM and the game | AF-GC-031 |
| M3 | Unity, assets/export, verified more complex samples and updates | AF-GC-034, AF-GC-036–038 |

M0 does not prohibit parallel work on independent M1 UI/setup tasks. Detailed dependencies below determine when a task can start; a release gate determines acceptance of the whole stage. The first game does not require every capability in the old AMM backlog.

**Next iteration:** 001, 002, 003, 005, 006, 039; 004 in parallel. After 006: 041/042. The demonstration must show actual CI results, no false READY status and a preserved description/draft. Then 007/008/011/025: a joint idea/setup walkthrough without actual spending. Priority does not replace a dependency.

**P0** blocks trustworthiness, control or authorisation; **P1** is needed for M1/M2; **P2** extends M3. **S/M/L** are preliminary relative estimates of up to 2/5/10 engineering days, without calendar promises. Split L work into smaller PRs before starting. assigned_role specifies the responsible discipline, not permission to run such an agent automatically.

Each task has one responsible discipline, an independent reviewer and an acceptance owner. Tracker states: Proposed → Ready (dependencies + scope + estimate) → In progress → In review → Accepted; Blocked includes a reason and next action. A failed test does not permit Accepted. No more than one active task per worker worktree; adjust WIP using measured resources.

<a id="список-задач"></a>
## Task list

| ID | Priority | Stage | Size | Outcome | Depends on |
|---|---|---|---|---|---|
| AF-GC-001 | P0 | M0 | M | Restore reproducible CI on three operating systems | — |
| AF-GC-002 | P0 | M0 | M | Show readiness only after actual checks | — |
| AF-GC-003 | P0 | M0 | S | Closing a dialog never confirms an action | — |
| AF-GC-004 | P1 | M0 | S | Preserve drafts and focus during automatic refresh | — |
| AF-GC-005 | P0 | M0 | M | Preserve the meaning of an ordinary game description | — |
| AF-GC-006 | P0 | M0 | M | Bind the selected model to actual execution | — |
| AF-GC-007 | P1 | M1 | M | Add a “My games” home screen and guided start | AF-GC-003, AF-GC-004 |
| AF-GC-008 | P1 | M1 | M | Turn an idea into an understandable first-game plan | AF-GC-005, AF-GC-007 |
| AF-GC-009 | P1 | M1 | L | Connect cloud AI through an understandable wizard | AF-GC-006, AF-GC-007, AF-GC-010, AF-GC-025 |
| AF-GC-010 | P1 | M1 | M | Store and revoke AI access without leaking keys | AF-GC-039 |
| AF-GC-011 | P1 | M1 | M | Check PC capabilities before selecting a model and engine | AF-GC-007 |
| AF-GC-012 | P1 | M1 | M | Recommend a realistic engine and AI configuration | AF-GC-008, AF-GC-011 |
| AF-GC-013 | P1 | M1 | M | Show an exact installation plan from verified sources | AF-GC-002, AF-GC-012 |
| AF-GC-014 | P1 | M1 | L | Execute and recover software installations | AF-GC-013 |
| AF-GC-015 | P1 | M1 | L | Start local infrastructure with one action | AF-GC-014 |
| AF-GC-016 | P1 | M1 | M | Create a Godot pack for the first 2D game | AF-GC-008, AF-GC-014 |
| AF-GC-017 | P1 | M1 | M | Validate a Godot project and produce a real build | AF-GC-016 |
| AF-GC-018 | P1 | M1 | M | Authorise a bounded cloud session with a transparent budget | AF-GC-009, AF-GC-010, AF-GC-025 |
| AF-GC-019 | P1 | M1 | L | Connect real development to the game plan | AF-GC-006, AF-GC-017, AF-GC-018, AF-GC-041, AF-GC-042 |
| AF-GC-020 | P1 | M1 | M | Preserve the latest verified game version | AF-GC-019 |
| AF-GC-021 | P1 | M1 | M | Launch “Play” for a specific working version | AF-GC-007, AF-GC-020 |
| AF-GC-022 | P1 | M1 | M | Turn post-play feedback into the next version | AF-GC-008, AF-GC-021 |
| AF-GC-023 | P1 | M1 | M | Explain progress and reliably stop work | AF-GC-019, AF-GC-020 |
| AF-GC-024 | P1 | M1 | M | Make the main journey accessible in Ukrainian and English | AF-GC-007, AF-GC-021, AF-GC-022, AF-GC-023 |
| AF-GC-025 | P1 | M1 | M | Define an eligible path for ages 12+ and adult participation | AF-GC-007 |
| AF-GC-026 | P1 | M1 | M | Accept the complete Godot journey on a clean PC | AF-GC-001, AF-GC-002, AF-GC-003, AF-GC-004, AF-GC-005, AF-GC-006, AF-GC-009, AF-GC-015, AF-GC-022, AF-GC-023, AF-GC-024, AF-GC-025, AF-GC-039, AF-GC-040 |
| AF-GC-027 | P1 | M2 | M | Install and verify local models | AF-GC-006, AF-GC-011, AF-GC-013, AF-GC-014 |
| AF-GC-028 | P1 | M2 | L | Give local AI a qualified development tool | AF-GC-017, AF-GC-027, AF-GC-041, AF-GC-042 |
| AF-GC-029 | P1 | M2 | M | Share PC resources between AI, the engine and the game | AF-GC-011, AF-GC-021, AF-GC-027 |
| AF-GC-030 | P1 | M2 | M | Route cloud/local work under explicit rules | AF-GC-018, AF-GC-028, AF-GC-029 |
| AF-GC-031 | P1 | M2 | M | Qualify local-only and hybrid game creation | AF-GC-026, AF-GC-028, AF-GC-029, AF-GC-030 |
| AF-GC-032 | P2 | M3 | M | Set up Unity Hub, Editor and required modules | AF-GC-013, AF-GC-014, AF-GC-025, AF-GC-026 |
| AF-GC-033 | P2 | M3 | L | Add a Unity pack, tests and build adapter | AF-GC-017, AF-GC-019, AF-GC-032 |
| AF-GC-034 | P2 | M3 | M | Accept the complete Unity journey for a beginner | AF-GC-022, AF-GC-023, AF-GC-024, AF-GC-033 |
| AF-GC-035 | P2 | M3 | M | Manage game asset provenance and import | AF-GC-016, AF-GC-020, AF-GC-026 |
| AF-GC-036 | P2 | M3 | L | Expand complexity through measurable reference games | AF-GC-031, AF-GC-034, AF-GC-035 |
| AF-GC-037 | P2 | M3 | M | Export a game and share it through a separate action | AF-GC-020, AF-GC-025, AF-GC-035 |
| AF-GC-038 | P2 | M3 | M | Update the application and collect understandable diagnostics | AF-GC-015, AF-GC-023, AF-GC-026 |
| AF-GC-039 | P0 | M0 | M | Align local API authorisation with the promised policy | — |
| AF-GC-040 | P1 | M1 | M | Test understandability with users aged 12–15 | AF-GC-009, AF-GC-015, AF-GC-022, AF-GC-023, AF-GC-024, AF-GC-025 |
| AF-GC-041 | P0 | M0 | M | Preserve roles and reviewer independence in a live mission | AF-GC-006 |
| AF-GC-042 | P0 | M0 | M | Qualify planning and bootstrap roles for providers | AF-GC-006 |
| AF-GC-043 | P0 | M2 | M | Atomically admit qualified workers with scoped attempts and shared capacity | AF-GC-039 |

<a id="як-читати-й-виконувати-задачі"></a>
## How to read and execute tasks

Intent and concrete acceptance criteria follow. The JSON additionally records components, validation methods, expected artifacts, Definition of Done and legacy mapping. The UI, API and recovery for a scenario belong to its task; they are not deferred to a final “draw the interface” step.

<a id="af-gc-001--відновити-відтворюваний-ci-на-трьох-ос"></a>
<a id="af-gc-001"></a>
### AF-GC-001 — Restore reproducible CI on three operating systems

A clean checkout must produce reliable test results regardless of the AI CLIs installed on the machine.

- Monitor tests do not depend on personal CLIs and separately check required/unnecessary providers.
- Subprocess termination checks use the appropriate OS mechanism; Linux/macOS do not invoke tasklist.exe.
- The Python 3.11/3.12 Windows/Linux/macOS matrix, wheel smoke and Docker CI pass or have an explicit, justified limitation without hidden skips.

**Validation:** Run on clean CI runners; retain complete summaries, skipped reasons, wheel/demo and cancellation evidence.

<a id="af-gc-002--показувати-готовність-лише-після-реальних-перевірок"></a>
<a id="af-gc-002"></a>
### AF-GC-002 — Show readiness only after actual checks

“Ready to create a game” must mean the selected path is ready, not merely that a database status changed.

- DEVELOPMENT does not start without a current required tools/services/model/workspace report for the approved plan.
- A missing program/model or failed probe produces a specific corrective action; rerunning checks the actual state.
- The UI distinguishes installed/authenticated/qualified/ready and simulated/live; an unselected provider does not block a working route.

**Validation:** Regression: an empty workspace without an engine cannot return environment READY; success/missing/stale/failed-probe cases.

<a id="af-gc-003--закриття-діалогу-ніколи-не-підтверджує-дію"></a>
<a id="af-gc-003"></a>
### AF-GC-003 — Closing a dialog never confirms an action

Remove the risk of inheriting a previous confirmation when reopening a native dialog.

- The dialog result resets before each opening; only an explicit button confirms the specific current request.
- Confirm → new dialog → Escape/Cancel sends no mutation request.
- Double-clicking and reopening do not duplicate an operation; focus returns to the initiating control.

**Validation:** Browser regression using a disposable fixture with intercepted requests; record zero mutations on Escape/Cancel.

<a id="af-gc-004--зберігати-чернетки-та-фокус-під-час-автооновлення"></a>
<a id="af-gc-004"></a>
### AF-GC-004 — Preserve drafts and focus during automatic refresh

Model or provider edits must not disappear every five seconds.

- Model/provider drafts, selection and focus survive at least three refresh cycles.
- A server-side change during editing displays a conflict and allows version selection.
- Status updates continue without replacing the whole form; Save and Cancel have explicit outcomes.

**Validation:** Browser: enter an unsaved model, wait three cycles, check value/focus and save/cancel.

<a id="af-gc-005--не-втрачати-зміст-звичайного-опису-гри"></a>
<a id="af-gc-005"></a>
### AF-GC-005 — Preserve the meaning of an ordinary game description

Importing a paragraph without Markdown must preserve user intent; heuristic import must not be labelled completed AI analysis.

- A Ukrainian/English paragraph is retained as an original source regardless of parser results.
- For “a cat collects coins, three lives”, the preview includes those requirements or asks for clarification; an empty filename-epic is not a ready plan.
- The UI truthfully labels deterministic import, AI proposal and confirmed plan; users edit the preview before import.

**Validation:** Text/PDF/Markdown import cases with semantic assertions for requirements, executable leaves and source trace.

<a id="af-gc-006--привязати-вибрану-модель-до-фактичного-запуску"></a>
<a id="af-gc-006"></a>
### AF-GC-006 — Bind the selected model to actual execution

Model selection must change the real provider request and retain truthful identity for independent review.

- Two permitted models from one provider produce the corresponding different qualified request/argv; arbitrary shell arguments are prohibited.
- An unsupported or unknown model is rejected before launch; logs record requested and effective models without secrets.
- Changing the model invalidates dependent qualification/permission; reviewer independence checks effective identity.

**Validation:** An adapter fixture compares model-a/model-b requests; a live canary uses only an explicitly permitted profile and budget.

<a id="af-gc-007--додати-головний-екран-мої-ігри-і-покроковий-старт"></a>
<a id="af-gc-007"></a>
### AF-GC-007 — Add a “My games” home screen and guided start

A beginner starts with an idea and sees only the current step: Idea → AI → Preparation → Creation → Play.

- The empty state has one main action, “Create a game”; task IDs, roles, leases and JSON are in the details.
- Steps, entered data and errors survive restart; users can return to an earlier step.
- An existing project shows the latest working version, current progress and next required action.
- Search and pagination provide access to more than 200 tasks/versions without manual API/JSON use; main filters offer lists of permitted values.

**Validation:** Browser journeys: clean state, interrupted wizard, returning to an existing game; API contract tests.

<a id="af-gc-008--перетворити-ідею-на-зрозумілий-план-першої-гри"></a>
<a id="af-gc-008"></a>
### AF-GC-008 — Turn an idea into an understandable first-game plan

The system preserves the ambition of a large game while agreeing on a small first playable milestone and subsequent steps.

- The brief includes genre, controls, objective/failure, platform, style, first playable and deferred features.
- Clarifications are specific and paced; assumptions, costs and result boundaries can be corrected before starting.
- The editable draft has executable tasks and game-specific criteria with source trace; live cloud planning becomes available after 018, and until then the UI does not declare an AI plan accepted.

**Validation:** Fixtures: platformer, top-down collector, puzzle, an oversized multiplayer request; independent brief and plan review.

<a id="af-gc-009--підключати-хмарний-ai-через-зрозумілий-майстер"></a>
<a id="af-gc-009"></a>
### AF-GC-009 — Connect cloud AI through an understandable wizard

Users identify products they already have; the wizard explains what connection each provides: API, supported CLI or no integration.

- A versioned catalogue shows the official login/device/API-key flow; a chat subscription does not automatically mean API access or credit.
- The first release qualifies at least one cloud coding route and an independent review route; others are shown as unsupported/requiring setup.
- Login occurs with the provider; after return, the connection check distinguishes auth, quota, model access, capability and network errors.

**Validation:** Disposable account/test keys: missing, expired, denied model, quota, offline, successful bounded canary; do not bypass service terms.

<a id="af-gc-010--зберігати-й-відкликати-доступ-до-ai-без-витоку-ключів"></a>
<a id="af-gc-010"></a>
### AF-GC-010 — Store and revoke AI access without leaking keys

A secret is entered in a dedicated step, stored through the OS and kept out of prompts, backlogs and support bundles.

- An OS credential-store reference survives restart; its value is injected only into an authorised process/request.
- Disconnect/revoke stops new calls and explains how to revoke access with the provider.
- Redaction is verified in errors, logs, exports, crash reports and screenshot instructions; browser APIs do not return the secret.

**Validation:** Search for a synthetic secret canary across all evidence/log/export sinks; revoke and restart tests on a supported OS.

<a id="af-gc-011--перевіряти-можливості-пк-до-вибору-моделі-та-рушія"></a>
<a id="af-gc-011"></a>
### AF-GC-011 — Check PC capabilities before selecting a model and engine

A read-only check before plan approval collects only necessary technical specifications and explains unknown values.

- The report records OS/architecture, CPU, available/total RAM, GPU/VRAM or shared memory, free disk space, runtimes/engines and check time.
- Unknown/unsupported is not converted into zero or an invented specification; a failed GPU probe does not block a cloud-only path.
- The UI shows what stays local; serial numbers, personal files and lists of unrelated processes are unnecessary.

**Validation:** Windows VM/host matrix: CPU-only, integrated GPU, discrete GPU, low disk; fixtures plus an actual inventory report.

<a id="af-gc-012--рекомендувати-реалістичний-рушій-і-конфігурацію-ai"></a>
<a id="af-gc-012"></a>
### AF-GC-012 — Recommend a realistic engine and AI configuration

Explain the recommendation and options for a low-powered PC, without promising that any model can create any game.

- Godot/local/cloud comparisons account for OS support, memory, disk, renderer and target build; Unity is explicitly a later stage until qualified.
- Download/cost/time estimates have a source, date and uncertainty range; unknown VRAM leads to a cautious recommendation.
- Users can select an available alternative; a justified first-milestone limit does not silently change their idea.

**Validation:** Decision-table tests on low-/mid-range PC profiles; review current engine/model catalogue requirements.

<a id="af-gc-013--показувати-точний-план-встановлення-з-перевірених-джерел"></a>
<a id="af-gc-013"></a>
### AF-GC-013 — Show an exact installation plan from verified sources

Before automatic setup, users see what will be installed, its source, purpose, destination and size.

- The catalogue pins version, official source, checksum/signature, dependencies, licensing step, disk space and change scope.
- The plan distinguishes already installed, reusable, install, update and manual action; it does not update unrelated software unnecessarily.
- Approval binds to the plan digest; changes to source/permissions/scope require a new decision, not an arbitrary shell script.

**Validation:** Plan diff, substituted checksum/source, dependency conflict, no-admin and offline fixtures.

<a id="af-gc-014--виконувати-та-відновлювати-встановлення-програм"></a>
<a id="af-gc-014"></a>
### AF-GC-014 — Execute and recover software installations

Lokvetia Core performs approved installations itself; people intervene only where personal action is required.

- Download/verification/installation/postcondition are logged; retry after interruption does not duplicate successful operations.
- The UI guides required UAC/login/EULA steps and resumes afterwards; it does not accept an agreement on the user's behalf.
- Insufficient disk space, a corrupt package, an occupied port and OS reboot have retry/repair/rollback paths that do not delete others' files.

**Validation:** Clean Windows VM: install Godot and required runtimes; kill/network loss/disk full tests in a disposable VM, reporting actual postconditions.

<a id="af-gc-015--запускати-локальну-інфраструктуру-однією-дією"></a>
<a id="af-gc-015"></a>
### AF-GC-015 — Start local infrastructure with one action

Users open the application without three PowerShell windows or manual database, server and worker setup.

- A packaged launcher installs/starts only required services, checks readiness and opens the wizard.
- If the selected path requires Temporal/Docker, setup checks prerequisites and starts them; a missing dependency has understandable guided recovery.
- Closing/updating/restarting does not lose games, leave uncontrolled processes or expose the service to the network.

**Validation:** Fresh Windows non-admin start, reboot/resume, port conflict, service crash, double-launch; a list of supported configurations.

<a id="af-gc-016--створити-godot-pack-для-першої-2d-гри"></a>
<a id="af-gc-016"></a>
### AF-GC-016 — Create a Godot pack for the first 2D game

Put game templates and rules in a separate pack, keeping the factory core project-neutral.

- The pack pins a supported Godot version, GDScript, scenes/assets/scripts structure and renderer for the baseline PC.
- Two minimal collector/platformer templates have controls, win/lose and restart without external content.
- The project opens in a real editor; pack import/upgrade does not overwrite authored files without a preview.

**Validation:** Create two new projects; actual Godot import/open; pack compatibility and asset licence checks.

<a id="af-gc-017--перевіряти-godot-проєкт-і-створювати-реальний-build"></a>
<a id="af-gc-017"></a>
### AF-GC-017 — Validate a Godot project and produce a real build

A positive textual verdict does not replace engine execution: import, script checks, runtime smoke and export are required.

- A qualified shell-free adapter checks version and flag support; import/parse/runtime/export have timeouts, exit codes and bounded logs.
- Missing export templates, syntax errors, missing resources and crashes produce failure; --check-only is not presented as a full game test.
- The artifact records commit, engine/template version, preset and checksum; successful headless smoke is complemented by a graphical run.

**Validation:** Godot CLI from official docs: healthy/broken reference projects, export preset/template mismatch, graphical playtest.

<a id="af-gc-018--дозволяти-обмежену-cloud-сесію-з-прозорим-бюджетом"></a>
<a id="af-gc-018"></a>
### AF-GC-018 — Authorise a bounded cloud session with a transparent budget

One understandable session permits sequential work within the selected AI, data, time and budget boundaries.

- A separate bounded CLOUD scope covers pre-start planning, coding and review; it does not disguise a remote provider as LOCAL or require a local model for M1.
- The UI shows provider/model, data to transmit, file scope and upper cost/time/iteration caps; new permissions/spending require a separate decision.
- Budget is reserved before a call, accounting for concurrency; quota/unknown cost/limit pauses do not launch hidden paid fallback.

**Validation:** Concurrency budget, denied scope, expired auth, cancellation, uncertain charge reconciliation; a capped real canary after setup.

<a id="af-gc-019--підключити-справжню-розробку-до-ігрового-плану"></a>
<a id="af-gc-019"></a>
### AF-GC-019 — Connect real development to the game plan

Executable tasks must change the game through a qualified worker, pass validation and produce integrated results.

- A dependency-ready task executes in a leased worktree with the required context; simulation is explicitly distinguished.
- The worker diff passes game validators and independent review; bounded repair stops when no progress is made.
- An accepted diff is integrated exactly once; child timeout/retry/restart does not duplicate a commit or paid call without reconciliation.

**Validation:** New Godot game: real worker → diff → import/test/build → independent review → accepted commit; failure/repair and replay evidence.

<a id="af-gc-020--зберігати-останню-перевірену-ігрову-версію"></a>
<a id="af-gc-020"></a>
### AF-GC-020 — Preserve the latest verified game version

Unfinished new work must not prevent users from playing the previous working version.

- A playable checkpoint includes commit, build digest, engine/assets/config and verification results.
- A failed subsequent build does not change the latest working pointer; promotion is atomic and replay-safe.
- Restoration creates a new branch/version with a preview; accepted history and originals are preserved.

**Validation:** Build failure, interrupted promotion, restore to prior version, duplicate acceptance and source/build identity checks.

<a id="af-gc-021--запускати-грати-для-конкретної-робочої-версії"></a>
<a id="af-gc-021"></a>
### AF-GC-021 — Launch “Play” for a specific working version

A prominent “Play” button opens the verified build and shows simple control instructions.

- The UI shows version/build ID, brief changes and controls; Play launches that exact artifact.
- The process has a controlled lifecycle, logs and Stop; a crash does not close the factory or lose progress.
- Users can play the previous version while the next one is developed; if resources are insufficient, AI pauses with an explanation.

**Validation:** Graphical Windows launch/stop/crash; latest working during a failed build; executable provenance/path checks.

<a id="af-gc-022--перетворювати-відгук-після-гри-на-наступну-версію"></a>
<a id="af-gc-022"></a>
### AF-GC-022 — Turn post-play feedback into the next version

Users write “make the jump higher” and the system changes the verified game while preserving the previous version.

- Feedback binds to the played build; text/permitted screenshot and reproduction steps have a preview.
- The system shows a short change plan and effects on earlier requirements; new spending or scope is not silently accepted.
- After development, users have a new build, verification of the specifically requested behaviour and a way to restore the previous version.

**Validation:** E2E: play v1 → higher jump → build v2 → human playtest → restore v1; stale feedback/conflicting request cases.

<a id="af-gc-023--пояснювати-прогрес-і-надійно-зупиняти-роботу"></a>
<a id="af-gc-023"></a>
### AF-GC-023 — Explain progress and reliably stop work

Users see what is happening, what they can already try and how to continue after a problem.

- The UI shows an understandable stage, latest heartbeat, blockers, spent/reserved budget and next action; an unknown ETA is labelled unknown.
- Pause/Resume/Stop cover scheduling, inference/build processes and spending; possible time needed to finish the current action is explained.
- After app/worker/PC restart, accepted builds, uncommitted edits and decision scope are preserved; an orphan is not relaunched without checking.

**Validation:** Fault injection at worker/build/acceptance boundaries, stop during a paid call, service restart and no-progress tests.

<a id="af-gc-024--зробити-основний-шлях-доступним-українською-та-англійською"></a>
<a id="af-gc-024"></a>
### AF-GC-024 — Make the main journey accessible in Ukrainian and English

Check the actual dynamic interface, keyboard use and understandable errors, not just HTML strings.

- All main creator-flow text is localised in uk/en; errors provide an understandable cause and action.
- Keyboard use, focus order/return, screen-reader labels/status, contrast and target size are checked in the rendered UI.
- 320 CSS px, 200% zoom and a laptop viewport do not hide controls; invalid responsive CSS is corrected.

**Validation:** Browser accessibility scan and keyboard suite, manual screen-reader walkthrough, responsive screenshots; applicable WCAG 2.2 AA criteria.

<a id="af-gc-025--визначити-доступний-шлях-для-12-та-участь-дорослого"></a>
<a id="af-gc-025"></a>
### AF-GC-025 — Define an eligible path for ages 12+ and adult participation

The product must support a 12-year-old creator without requiring circumvention of external services' age or account rules.

- Each connector documents current official requirements and verification date; eligibility is checked before offering login.
- An understandable adult setup/budget/consent flow exists where permitted; if the cloud route is unavailable, M1 offers to save the idea or use an offline template demo, while actual local AI is explicitly assigned to 027–031.
- Personal data are minimised, cloud transmission and retention are understandable; publication, purchases and key sharing are not defaults.

**Validation:** Review selected providers' official terms and relevant launch requirements; UX cases for 12,13–15,16+ without unnecessary personal data collection.

<a id="af-gc-026--прийняти-повний-godot-шлях-на-чистому-пк"></a>
<a id="af-gc-026"></a>
### AF-GC-026 — Accept the complete Godot journey on a clean PC

M1 ends with a reproducible real game, not a count of implemented services.

- Clean Windows PC: installation → idea → authorised cloud AI → setup → Godot build → Play → feedback → v2 → restore.
- At least a collector and a platformer complete the journey; offline/no quota/low disk/crash have verified recovery.
- The report includes host/versions/cost/time and actual video/logs; automated tests and research gate 040 pass; no P0 issues remain open.

**Validation:** Two clean supported configurations, owned authorised test accounts with a cap; independent acceptance.

<a id="af-gc-027--встановлювати-та-перевіряти-локальні-моделі"></a>
<a id="af-gc-027"></a>
### AF-GC-027 — Install and verify local models

The system recommends a compatible local runtime/model, downloads the approved model and checks actual inference.

- The first local route through Ollama has a model catalogue with licence, digest, disk/context/RAM assumptions and qualification date.
- Downloads support progress/cancel/resume; an existing model is reused after checking its version.
- CLI availability is not a loaded model; actual canary/OOM/offline checks determine readiness and offer an option for a weaker machine.

**Validation:** Official Ollama runtime on CPU-only and discrete GPU, interrupted download, OOM, bad digest, missing model.

<a id="af-gc-028--дати-локальному-ai-кваліфікований-інструмент-розробки"></a>
<a id="af-gc-028"></a>
### AF-GC-028 — Give local AI a qualified development tool

Local text generation becomes a controlled worker with file tools, isolation, validators and the ability to repair errors.

- A qualified local worker edits only the task worktree through permitted tools; advisory completion is not called an implemented game.
- Role/capability/model profiles are compatible with planning/development/review contracts; unsupported tasks are explained.
- A local-only Godot change passes an actual build and independent review; if no other qualified model is available, the status requires explicit human review.

**Validation:** Real local model on a reference machine: write → build → repair; tool injection/path escape/timeout/OOM negative suite.

<a id="af-gc-029--ділити-ресурси-пк-між-ai-рушієм-та-грою"></a>
<a id="af-gc-029"></a>
### AF-GC-029 — Share PC resources between AI, the engine and the game

Concurrent local tasks must not consume all memory and make playtesting impossible.

- The scheduler accounts for host-wide RAM/VRAM reservations, queue and actual load; a per-mission lease is not presented as a GPU scheduler.
- Playtesting can free resources through AI pause/unload; local time/context/disk caps apply even without monetary cost.
- OOM, stale leases and multiple missions do not cause endless reload; retry/backoff and diagnostics are retained.

**Validation:** Two competing missions plus Godot Play; RAM/VRAM pressure, crash/restart and queue fairness on a qualified PC.

<a id="af-gc-030--маршрутизувати-cloudlocal-за-явними-правилами"></a>
<a id="af-gc-030"></a>
### AF-GC-030 — Route cloud/local work under explicit rules

Users choose local-only, cloud-only or hybrid and understand where each task will go.

- HYBRID has separate provider/model/data/tools/cost authorisation; LOCAL never permits remote fallback.
- Quality/capability, privacy and remaining budget are checked before changing route; the UI explains the reason and effective model.
- Cloud unavailability or local OOM produces permitted fallback or a pause; work and independent reviewer identity are not lost.

**Validation:** Local-only network-denied test, cloud outage, budget exhaustion, model replacement and a stale capability matrix.

<a id="af-gc-031--кваліфікувати-local-only-та-hybrid-створення-гри"></a>
<a id="af-gc-031"></a>
### AF-GC-031 — Qualify local-only and hybrid game creation

M2 must demonstrate that a game is created and improved using local resources and an authorised mixed configuration.

- Local-only and hybrid complete idea→build→play→feedback on documented hardware/model profiles.
- Peak memory, latency, quality, cost and OOM recovery are measured; a weaker PC receives an honest limitation/alternative.
- Recovery causes no unauthorised cloud traffic, checkpoint loss or repeated accepted mutation.

**Validation:** Actual local hardware matrix and fault campaign; independent artifact review and network/cost evidence.

<a id="af-gc-032--підключати-unity-hub-editor-і-потрібні-модулі"></a>
<a id="af-gc-032"></a>
### AF-GC-032 — Set up Unity Hub, Editor and required modules

Unity has its own guided setup and qualified version; do not assume every Hub action and activation can be automated.

- The catalogue pins supported Unity Editor/Hub versions and platform modules; it detects existing installations and project compatibility.
- Account/licence/EULA steps are handed to the person through the official flow; the system checks the result and resumes setup.
- Insufficient licence, missing module, low disk and conflicting editor versions have actionable recovery.

**Validation:** Clean supported Windows VM with a legitimate test licence; activation handoff and install/resume evidence.

<a id="af-gc-033--додати-unity-pack-тести-та-build-adapter"></a>
<a id="af-gc-033"></a>
### AF-GC-033 — Add a Unity pack, tests and build adapter

The Unity C# pipeline uses shared contracts but separately checks editor imports, compilation and the real game.

- The reference project has gameplay, controls and win/lose; package lock and Editor version are reproducible.
- Batch build, EditMode/PlayMode checks and export use qualified vectors, logs/results and timeouts; an identical exit code does not hide compilation errors.
- Graphical Play and artifact identity integrate with existing checkpoint/feedback controls; local/cloud support is explicitly stated.

**Validation:** Healthy/broken Unity projects: C# compile error, missing asset/package, test failure, real build and graphical launch.

<a id="af-gc-034--прийняти-повний-unity-шлях-для-новачка"></a>
<a id="af-gc-034"></a>
### AF-GC-034 — Accept the complete Unity journey for a beginner

Unity support means a completed creation, play and revision sequence on a clean configuration.

- Setup→idea→AI→Unity project→test/build→Play→feedback→v2 completes with the previous version preserved.
- Licensing/network/compile/crash failures show simple recovery and do not declare the build ready.
- An Editor/platform/model/hardware matrix and measured limits are published; Godot regressions pass.

**Validation:** Clean supported Unity machine, recorded human playtests and an independent qualification report.

<a id="af-gc-035--керувати-походженням-та-імпортом-ігрових-ресурсів"></a>
<a id="af-gc-035"></a>
### AF-GC-035 — Manage game asset provenance and import

Images, models, sound and fonts have safe import, provenance and understandable usage restrictions.

- Every asset has source/licence/attribution or an unknown marker; unknown blocks sharing/export that requires rights.
- Import checks type/size/malicious archive paths; unsupported formats do not execute arbitrary plugins.
- Texture/poly/audio size budgets are tied to target hardware; asset changes have preview and rollback.

**Validation:** Licensed fixture assets, oversized/corrupt archives, missing attribution and engine reimport/performance checks.

<a id="af-gc-036--розширювати-складність-через-вимірювані-зразки-ігор"></a>
<a id="af-gc-036"></a>
### AF-GC-036 — Expand complexity through measurable reference games

Turn “almost any complexity” into support levels verified by real projects.

- The catalogue covers a simple 2D, multi-level 2D and small 3D game with save/load, UI, audio and asset budgets.
- Each level has known engine/model/hardware limits, quality/performance targets and playable acceptance criteria.
- Multiplayer/open-world/console/VR are labelled separate future investigations; the system proposes a scoped prototype without a false guarantee.

**Validation:** Reference-game benchmark: playability/performance/build regression plus manual gameplay review at each level.

<a id="af-gc-037--експортувати-гру-та-ділитися-нею-окремою-дією"></a>
<a id="af-gc-037"></a>
### AF-GC-037 — Export a game and share it through a separate action

A finished build can be downloaded locally; publication has a separate target, preview and understandable user decision.

- Supported target presets create a versioned package with attribution, checksum and brief launch instructions.
- Secrets, local paths and unnecessary personal data are excluded from export; unsupported platforms are explained before building.
- Publication/visibility/external access is never inherited from development permission; cancelled sharing does not change external state.

**Validation:** Launch the export on a clean machine, inspect the package and test simulated publication preview/denial; live publication only on a separate request.

<a id="af-gc-038--оновлювати-застосунок-і-збирати-зрозумілу-діагностику"></a>
<a id="af-gc-038"></a>
### AF-GC-038 — Update the application and collect understandable diagnostics

Owners must be able to update the factory safely, recover a game and get help without manually hunting for logs.

- A signed update with backup/migration/rollback checks project compatibility and does not change pinned engines/models without a separate plan.
- A support bundle has preview/redaction and versions, but no keys, prompts or game files without explicit selection.
- Uninstall distinguishes the application from user games; projects are preserved by default and open after reinstall.

**Validation:** Upgrade a previous supported version, interrupted migration, rollback, secret canary export, uninstall/reinstall on a VM.

<a id="af-gc-039--узгодити-авторизацію-локального-api-з-обіцяною-політикою"></a>
<a id="af-gc-039"></a>
### AF-GC-039 — Align local API authorisation with the promised policy

All control surfaces must enforce one documented access contract; protection must not depend on the particular screen.

- An inventory of read/mutation endpoints defines required auth and scope; configured token/session policy applies consistently.
- The browser has a supported session flow; actor identity, local origin/host, expiration/revoke and required human gates are checked server-side.
- A negative matrix of absent/incorrect/expired access passes; sensitive reproduction details go through private security review.

**Validation:** Endpoint authorisation matrix, intended browser flow, origin/host denial, replay and regression tests without publishing tokens.

<a id="af-gc-040--перевірити-зрозумілість-із-користувачами-1215-років"></a>
<a id="af-gc-040"></a>
### AF-GC-040 — Test understandability with users aged 12–15

Usability is established by observing beginners, not by a developer assumption or API test.

- A pilot includes at least 5 beginners aged 12–15 with appropriate adult consent; synthetic data and authorised accounts.
- Target: ≥4/5 find the starting point and agree on an understandable brief within ≤5 minutes; ≥4/5 launch the build, give feedback and find Stop without moderator prompts.
- The report separates active user time from download/build/AI waiting and records help/errors; usability blockers are fixed and retested.

**Validation:** Moderated scenarios and an anonymised protocol; this is a pilot gate, not statistical proof for all children.

<a id="af-gc-041--зберігати-ролі-та-незалежність-reviewer-у-live-mission"></a>
<a id="af-gc-041"></a>
### AF-GC-041 — Preserve roles and reviewer independence in a live mission

Child authorisation must not replace every stage with one agent/model or turn self-review into an independent verdict.

- Implementation, validation, proxy review and policy stages receive separate compatible scoped assignments.
- The effective producer identity is excluded from independent review; without a reviewer, the mission waits for a decision instead of self-accepting.
- Replay/replace/resume preserve stage-identity provenance and do not expand child permissions.

**Validation:** A code-model/review-model/policy-model fixture checks different invocation identities; same-model and mismatch denial before subprocess launch.

<a id="af-gc-042--кваліфікувати-ролі-planning-та-bootstrap-для-провайдерів"></a>
<a id="af-gc-042"></a>
### AF-GC-042 — Qualify planning and bootstrap roles for providers

An approved route must actually support planning/development roles, rather than hit an allowlist block after starting.

- Role IDs/capabilities/profile compatibility is checked before route selection and mission approval.
- The first supported provider passes read-only planning and the required bootstrap/developer contract without blanket tool permissions.
- An unsupported role displays a specific available alternative; negative tests confirm that prohibited roles remain prohibited.

**Validation:** Matrix of shipped provider profiles × autonomous role IDs; bounded canary for qualified pairs, 0 subprocess for rejected pairs.

<a id="af-gc-043"></a>
### AF-GC-043 — Atomically admit qualified workers with scoped attempts and shared capacity

One admission transaction checks the current qualification, lifecycle, trusted project owner and physical worker capacity, then creates the existing assignment, lease and first attempt.

- Share capacity across logical aliases and projects; worker-supplied capacity is not authority.
- Bind the exact task, run, stage, attempt, ready worktree and context before launch. Require the stored admission even when optional launch metadata is omitted.
- Replay one request or start without another attempt or external launch. Retain uncertain capacity after lease expiry, heartbeat loss or a lost start response.
- Release only with trusted stop evidence for the exact admission and fence; old acknowledgements cannot free newer work. Preserve the unregistered local API.

**Validation:** Independent SQLite connection races, rollback injection, synthetic runtime start/replay, stale qualification and scope denial, affected legacy tests, and independent review. This does not certify a remote host or deployment. See [worker admission](worker-admission.md).

<a id="що-робимо-зі-старим-беклогом"></a>
## What happens to the old backlog

Stable `AF-001…057` and `AF-AMM-001…048` are preserved. They are not deleted, renumbered or imported again as new work. “57/57” describes earlier platform requirements, not readiness of a product for children. New IDs denote new outcomes or specific regressions; legacy references mean reuse/elaboration, not automatic fulfilment of a dependency.

| Earlier requirements | Decision | New outcome |
|---|---|---|
| AF-036–043 | Reuse the API/UI foundation; add the creator flow and browser evidence | 003–004, 007, 021–024, 040 |
| AF-009–016; AMM-007–011 | Reuse intake/revisions; retain the actual idea and a short game brief | 005, 008, 022 |
| AF-004/018/019/056; AMM-005/006/029 | Preserve LOCAL; add a separate bounded CLOUD/HYBRID scope | 010, 018, 025, 030, 039 |
| AMM-020–022, 035–040 | Demonstrate through actual changes and playable checkpoints | 019–023, 041–042 |
| AMM-023–029 | Combine into hardware-aware local worker/resource outcomes | 011–012, 027–031 |
| AMM-030–034 | Separate early read-only inventory from approved installation execution | 002, 011–015 |
| AMM-041–045 | Distribute API/UI work across sequential user scenarios | 007–024, 030 |
| AMM-046–048 | Check failures in every task; separate release gates | 001, 026, 031, 034, 040 |
| AF-029–035 | Preserve implemented contracts; defer further multi-tenant/cluster rollout | Not a prerequisite for the first game |

Further expansion of the marketplace, complex agent debates/quorums, clustered deployment, long-term soak and arbitrary engines does not precede the first playable. Existing mechanisms are not removed. Multiplayer, console publishing, VR, billing/marketplace and unrestricted unattended operation require separate research after the main journey is qualified.

<a id="перевірка-й-імпорт"></a>
## Validation and import

From the repository root after installing the project:

```sh
python scripts/validate-game-creator-backlog.py
python -m agent_factory backlog validate --path examples/game-creator-backlog.json
```

The manifest is a plan, not automatic permission for AI spending, installation or GitHub mutation. It can be imported using existing Lokvetia Core tools after validation. This change commits documents to Git; it does not create dozens of GitHub Issues or start executing the backlog.

<a id="первинні-джерела-для-реалізації"></a>
## Primary sources for implementation

Checked 2026-09-05; pin the selected version during implementation instead of relying on moving `stable` documentation.

- [Godot command line](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html): separate import/headless/script/export mechanisms and export presets/templates; this is why 017 requires more than a positive agent verdict.
- [Unity Editor command line](https://docs.unity3d.com/6000.0/Documentation/Manual/EditorCommandLineArguments.html): editor automation arguments; the Unity adapter and licence/setup are qualified separately in 032–034.
- [Ollama FAQ](https://docs.ollama.com/faq): memory/context/concurrency and model loading must inform local planning; 027–031 verify this on a real PC.

These sources support adapter requirements; they do not prove Lokvetia Core already implements them. AI account terms, licences and age requirements are checked separately in 025 before enabling a connector.
