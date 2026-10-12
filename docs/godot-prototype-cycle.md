# Local platformer proof

[Українською](godot-prototype-cycle.uk.md)

`lokvetia-prototype` implements a small, explicit local capability: a text idea
becomes a playable Godot 2D platformer; a subsequent text request creates another
verified Windows EXE. It uses a real installed Ollama model to produce a strictly
validated specification, then compiles that data with trusted Godot templates.
The model does not supply executable code or shell commands.

Supported parameters are the title, movement speed (220–300), jump impulse
(520–560), and one or two jumps. The level has four fixed platforms, a goal,
fall detection and restart. Other mechanics must be reported as unsupported.
This does not establish arbitrary game generation or complete the general
multi-role Studio executor. The website and desktop GUI do not yet invoke this
command. Existing planning-only mandates remain planning-only.

## Run two versions

Use Windows with Git, Python 3.11+, Core installed, an already installed
`qwen2.5-coder:7b` or `qwen2.5-coder:14b` model served by Ollama on
`127.0.0.1:11434`, and a supported Godot console executable with matching Windows
export templates. A desktop session is required for rendered EXE acceptance.
No model download, paid provider, remote build host or service installation is
performed by this command.

The `--execute-local` flag authorizes exactly one bounded local inference,
generated source snapshot, local Git commit, build and exported game test. It
must be supplied for each new request. Without it, no files or inference are
created. Nothing is pushed or published by the command.

```powershell
$godotExe = 'C:\Tools\Godot\Godot_console.exe'
$gameRoot = Join-Path $PWD 'my-platformer'
python -m agent_factory.godot_prototype create --root $gameRoot --request 'Create a simple platformer called Sky Steps, with normal jumping and a green goal.' --command-id first --godot $godotExe --execute-local
$first = Get-Content (Join-Path $gameRoot 'revisions\first\result.json') -Raw | ConvertFrom-Json
& $first.artifact
python -m agent_factory.godot_prototype revise --root $gameRoot --request 'Add double jump.' --base-version $first.version --command-id double-jump --godot $godotExe --execute-local
$second = Get-Content (Join-Path $gameRoot 'revisions\double-jump\result.json') -Raw | ConvertFrom-Json
& $second.artifact
```

The installed `lokvetia-prototype` entry point accepts the same arguments.
Arrow keys move; Space or Enter jumps and restarts a finished round. The first
platform is solid: jump upward beside it, then move onto it near the jump apex.
The EXEs embed their game data; players need neither Godot nor Python nor Ollama.

## What is verified

Each revision stores the original request, base version, exact local model
digest, response, validated specification, source Git commit and build logs in
its own directory. Source snapshots and previous EXEs are preserved. These local
records can contain private user requests and must not be committed by default.

Godot imports the project, checks all scripts, runs a headless smoke test and
exports Windows x64. The exact exported EXE then runs in a Windows window with
an opt-in input/physics driver. Acceptance checks movement, floor collisions,
ground jump, requested air-jump behavior, refusal of extra jumps, landing and
jump reset, goal and fall outcomes, and both restarts. A final route completes
the entire level from spawn using input only. The goal/fall boundary checks
separately reposition the player; the final route and jump checks do not.

`evidence/runtime.json` contains measured vertical velocities and route positions;
`evidence/game.png` is a frame captured by the running EXE. The EXE checksum is
checked after execution and recorded in the existing playable-version ledger.
Only a fully passing candidate moves the current pointer. Automated rendered
acceptance is recorded separately from a manual graphical playtest; the latter
remains an explicit evidence gap.

Reusing a successful command with identical arguments returns its receipt without
new inference. Changed requests, stale base versions, tampered EXEs, no-op edits,
malformed model data and failed tests are rejected. Failed/interrupted attempts
retain evidence and require a new command ID. A crash after ledger promotion but
before the final receipt is recovered without rebuilding or repeating inference.

## Limits and tests

Inference uses a fixed loopback endpoint without proxy or redirects, an exact
installed model digest, a 180-second timeout, a 384-token output ceiling and a
32 KiB response bound. There is no automatic paid fallback or unbounded retry.
Each engine operation has its adapter timeout; exported EXE acceptance is limited
to 40 seconds. Local project locking prevents competing revisions.

```powershell
python -m unittest tests.test_godot_prototype tests.test_godot_pack tests.test_godot_engine tests.test_playable_versions -q
```

Unit boundary tests use fake tools and do not establish that Godot or a model
worked. Live acceptance evidence for the demonstrated two-EXE cycle is recorded
in [the proof report](godot-prototype-proof.json).
