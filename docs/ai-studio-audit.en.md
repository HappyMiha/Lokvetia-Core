# AI studio implementation audit

<!-- translation-metadata:start -->
<details>
<summary>Translation source and currency</summary>

Translation source: [ai-studio-audit.uk.md](ai-studio-audit.uk.md). Source SHA-256 (UTF-8/LF): `2554e0fc878e9f77c1955906c00a84957dcf61a0a5d91d56535f74a251af03c2`.

</details>
<!-- translation-metadata:end -->

[Українська](ai-studio-audit.uk.md). Checked 13 September 2026.

The complete **idea → autonomous development → playable slice** scenario is not
implemented yet. Modules and passing tests do not establish product acceptance.
The [AI studio specification](ai-studio.en.md) is the requirement source. Backlog
IDs and acceptance statuses are unchanged.

| Requirement | Available | Still needed for acceptance |
|---|---|---|
| F1: one button | `/api/studio/create` creates a mission, records the authenticated owner's mandate and queues sequential local planning. Replays reuse the mission. | `CoreMissionDriver.approve` still refuses. Bind a verified revision, isolated repository, epoch and real executor. This request does not write the game automatically. |
| F2: clarification | Lokiravia preserves the original, fields and answers. | Defaults and the decision about another clarification block are not connected to the autonomous loop. |
| F3: backlog | Full grouped backlog with states; visible studio tabs refresh every five seconds. | A verified game plan produced by a real model. |
| F4: slices | Godot adapter, templates, version ledger and evidence checks exist. | Connect stage execution to artifact delivery and launching that exact version. |
| F5: pause | Supervisor checks pause and disposition before another step; comments and cycles persist. | Pause within multi-role planning, automatic remaining-work replanning and applying comments to the executable backlog. |
| F6: team | Role catalogue and addition consequences are available. | Complete model assignment, executor routing and new QA-task integration. |
| F7: installation | Wheel/sdist and distribution checks; local worker qualification and a Windows launcher added. | Native installers for three operating systems, signed releases and clean-machine update proof. |
| F8: machine | Build/hardware reports identify their machine; a web container cannot register itself as the user's PC through this route. | Cloud-route product evidence. This qualification uses a local PC. |
| F9: paid tools | Decision ledger and refusal/alternative choices exist. | Automatically apply restrictions to the remaining plan. Platform purchases are not implemented. |
| Cost | Ledger, forecast and limits; local start grants zero budget for paid calls. | Actual billing from every provider and reservations for each subtask. |
| Recovery | Processes cannot drive the same mission concurrently. Later polls do not replay an interrupted step with unknown effects. | A full operator resolution protocol for uncertain results and executor recovery. |

## Changes made during this audit

- Check pause, stopped missions, the owner and the mission key before execution.
- Expired, future or malformed mandates cannot run; reject NaN, infinity and
  negative budgets. A zero entered in the UI no longer becomes unlimited.
- A separate execution lock leaves the state database available for Pause.
- Local start requires real API/CLI qualification, the exact installed model
  digest and the unchanged provider profile. A `verified` flag alone is insufficient.
- Planning requests now include nested output fields before the first inference;
  previously those fields were exposed only through validation failures.
- Add an owner- and revision-bound local Lokiravia → Core bridge.

## Evidence and its limits

Regression checks cover the supervisor, HTTP launch, replay, owner substitution,
source qualification and the Lokiravia bridge. Chromium checks the studio in
Ukrainian and English, including a narrow viewport. Backlog, translation and
planning pipeline checks run separately.

A local Windows worker ran seven Ollama API role checks and seven CLI checks.
Real planning exposed missing nested contracts and the need for explicit
measurable acceptance criteria. Passing a short canary does not prove a model can
complete a large plan or create a game. Live trial records stay in the private
workspace and are not committed.

The bundled `collector-2d` passed Godot import, GDScript checks, headless runtime,
Windows exe export and a headless launch of that exe. This qualifies tooling on
a real worker; it is not a model-created arbitrary game or a graphical playtest.

Both supplied test domains returned Cloudflare 1033 during inspection. A branch
push is not evidence of site deployment or end-to-end scenario acceptance.

None of six full planning trials with Qwen 7B produced a verified plan: trials
stopped on the JSON contract or acceptance criteria. Do not weaken validation
to label such a result ready. Core and Lokiravia wait for their shared model
without the ordinary ten-second write timeout dropping a queued request.

## Running locally

Install Core and Lokiravia in the same Python environment. Keep private data
outside both repository checkouts. With Ollama, a supported model and Godot
already installed on the PC:

```powershell
python -m agent_factory.studio_local_worker --workspace ..\local-studio --actor operator --godot C:\Tools\Godot\godot.exe --run-live
.\scripts\start-local-studio.ps1 -Python ..\.venv\Scripts\python.exe -Workspace ..\local-studio -WithLokiravia
```

`--run-live` runs bounded local qualification requests. Without it, setup only
scans the PC and records an unverified source. It does not download a model or
accept credentials. The launcher binds only to loopback and refuses occupied
ports; `-CorePort` and `-CreatorPort` select alternatives.

The next critical connection is a real executor after verified planning, bound
to a separate mission repository and Godot evidence. Until then, completing
planning must not be described as completing game creation.
