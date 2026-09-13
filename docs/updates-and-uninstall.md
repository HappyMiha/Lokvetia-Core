# Updating the application, and uninstalling without losing a game

An update may fix the factory; it must never quietly take a creator's game with
it.

For the user-facing **Check for updates → Download Windows EXE** journey, see
[Studio updates](studio-updates.md). It checks official releases and downloads an
installer for the user to run; the signed update engine below remains a separate mechanism.

Requirement trace: the update and uninstall half of `AF-GC-038`. The diagnostics
half is in [support bundles](support-bundle.md). Applying a real update still
needs an installer to supply the steps — Core owns the gate, not the installer.

## Before anything moves

An update is a signed manifest: version, the minimum installed version it
supports, the schema version it targets, the engine and model versions it pins,
its migrations, and an HMAC signature from a named trust root.

`plan` refuses, naming the reason:

- the signature does not verify against an approved trust root, or the manifest
  was altered after signing;
- the update is not newer than what is installed, or needs a newer base than
  what is installed;
- the update targets a schema older than the one already applied;
- **it would move an engine or model that a project has pinned.**

That last one is a separate decision, not a side effect of updating. The plan
reports the exact change (`godot 4.3 -> 4.4`) and which projects are affected,
and it stays blocked until an approved plan is named with `--separate-plan`. One
approved plan covers every project that shares that pin; a project pinned to a
different engine is not affected at all.

## Applying it

`apply` runs only an allowed plan, and:

1. takes a backup first — an update without a recorded backup never runs;
2. runs the declared steps in order;
3. **rolls the whole update back on any failure**, restoring the backup and
   recording which step failed and why.

The receipt records the outcome (`applied`, `rolled_back` or `refused`), the
versions before and after, the backup, the completed steps and the approver.

## Uninstalling

The plan separates the application's own state from user projects. Projects are
**preserved by default**, and the preview says so in plain words. Removing them
needs the plan to ask for it *and* a second explicit confirmation at the moment
of removal; without both, nothing is removed. A project that lives inside the
application's own state cannot be separated safely, so the plan refuses rather
than guessing.

```bash
lokvetia update plan --manifest ./update.json \
    --project-pin "my-game:godot:4.3" --trust-key release --trust-secret "$TRUST"
lokvetia update plan --manifest ./update.json \
    --project-pin "my-game:godot:4.3" --separate-plan engine-migration-2026-09 \
    --trust-key release --trust-secret "$TRUST"

lokvetia uninstall plan --project ./games/collector
```

`update plan` exits `3` when the update is blocked. `uninstall plan` previews
only — the CLI removes nothing.

## What this does not do

It does not download an update, install one, or decide what a step should be:
the caller supplies the backup, restore and steps. Passing trust material on the
command line is for testing; a real deployment supplies it from the operator's
own key storage.
