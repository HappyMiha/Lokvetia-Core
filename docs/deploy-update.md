# Updating the machine that deploys

The controller polls main and rolls out every new revision on its own. The
controller *itself* is not part of what it rolls out: `ops/test-deploy/Install.ps1`
copies `scripts/autodeploy.py` and the runtime bundle out of a checkout, and the
machine then runs those copies until somebody runs the script again.

That gap has already cost a rollout. Two releases failed on HappyDucky02 with
`Existing database schema would change` and `Retained-release limit reached`,
and both fixes were already on main — in the controller files the machine had
never been given. Nothing on the page said so, so the failures read like faults
in the release.

## What the page now tells you

The controller publishes, with every cycle:

- **the revision it was installed from**, and when — or, for an installation made
  before this record existed, that the revision was not written down;
- **every controller file the revision it is deploying has moved past**, by path.

When that list is not empty, the deployment page prints it above the cards, and
any failure while it is not empty carries one more sentence: the controller on
this machine is older than the revision it is rolling out, these files differ,
re-install before reading the failure as a fault in the release. It is a
statement about the machine, never a verdict about the release, and it never
blocks a rollout — an older controller that still works is allowed to work.

The comparison ignores line endings, so a checkout made on Windows does not read
as different from the copy beside it, and it skips a file the revision does not
carry: a checkout older than the controller is not the controller's business.

## Updating

On the deployment machine, as the account that owns Docker Desktop:

```powershell
git -C <checkout> fetch origin
git -C <checkout> checkout main
git -C <checkout> pull --ff-only
& <checkout>\ops\test-deploy\Install.ps1 -ServerRoot <server root> -Python <python.exe>
```

The script now:

1. **stops a running controller before replacing the files it runs from.** A
   re-registered scheduled task ignores a second start, so without this the
   machine kept running the old controller after a "successful" re-install. An
   interrupted rollout is recovered on the next start — routing returns to the
   previous release and the attempt is recorded as a failure — so this leaves no
   half-activated release behind.
2. **refreshes** `controller.py`, both dashboards, the progress runtime and the
   runtime bundle, and records the revision it took them from in `installed.json`.
3. **reconciles an existing `config.json` instead of skipping it.** Only keys the
   file does not have are added; a value somebody set on purpose is never
   overwritten, and the previous file is kept beside it as `config.json.previous`.
   Adding a key to the installer is therefore enough for it to reach a machine
   that was installed long ago.
4. restarts the controller.

`-ConfigureOnly` does all of that without touching Docker volumes, the gateway or
the applications; it restarts the controller if it was running.

## What updating does not do

- **No application container is restarted.** The controller reaches the new
  releases on its next cycle, the same way it reaches any other revision.
- **No secret is regenerated.** Every file under `secrets/` is written once and
  left alone afterwards.
- **A key an operator changed is not reset**, including one this release would
  have set differently. If a machine needs a changed value back, change it in
  `config.json`; the installer will not argue with it.
- **`initial_routes` and `progress` are not reconciled key by key.** They are
  written on a fresh machine and left whole on an existing one, because both are
  nested state an operator is expected to own.
