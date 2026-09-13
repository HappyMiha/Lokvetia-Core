# What you downloaded, and whether it is what we published

Core could not be downloaded and installed locally, which is odd for a product
whose whole design assumes a local run. This is the first half of fixing that:
the files, and a way to check them.

Requirement trace: `AF-ST-101` (epic `AF-ST-E1`). It is not labelled accepted;
what is still missing is at the bottom.

## Building a release

```bash
python scripts/build_distribution.py --out dist
```

It builds the wheel and the source archive, then **installs the wheel into a
clean virtual environment and asks the installed command its version**. A
release nobody installed is not a release, and that check is the only thing
that lets the manifest say this system was tried.

The output directory gets a `distribution.json` stating, for every file, its
name, size and SHA-256, plus the Python the release needs and the systems it
was actually tried on. Building without the install check (`--skip-install-check`)
produces the files but claims no system.

`--tried-on windows` exists for a person who really did install it on Windows.
It is the one way to make this file say something untrue, which is why the
default is nothing.

## Checking a download

```bash
lokvetia download show   --manifest distribution.json --platform windows --language en
lokvetia download verify --manifest distribution.json
```

`verify` refuses anything it cannot vouch for:

- a file that is not there;
- a file of the wrong size — refused before a byte is hashed, because the
  cheap check is also the clearer message;
- a file whose SHA-256 does not match, naming both digests and saying not to
  install it.

`show` prints what the release says about itself, including a line for **each**
supported system: either "the release was tried on linux" or "this release has
not been tried on windows yet. It may work; we have not checked."

## A checksum is not a signature

Every rendering of a release repeats it: a checksum proves the file did not
change on the way; it proves nothing about who published it. Signed updates,
with a trust root, backups and rollback, are
[`application_update`](../src/agent_factory/application_update.py), and this
module deliberately does not invent a key of its own.

## What is not claimed

- **Windows desktop preview is separate.** See [desktop installation](desktop-installation.en.md) for the standalone exe and download page. There is no `.msi`, `.pkg` or `.deb` here — what
  exists is a wheel, a source archive, a manifest, and a check. Producing and
  signing native installers needs signing identities this repository does not
  have.
- **Desktop download page:** `/downloads` serves the separately published desktop manifest. The wheel/source manifest described here remains available for technical installations.
- **The install check proves installation, not use.** It installs the wheel and
  runs `lokvetia --version` on the build machine. That is a real check, and it
  is all it is.
