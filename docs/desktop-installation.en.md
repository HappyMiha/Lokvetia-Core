# Install Core on another PC

<!-- translation-metadata:start -->
<details>
<summary>Translation source and currency</summary>

Translation source: [desktop-installation.uk.md](desktop-installation.uk.md). Source SHA-256 (UTF-8/LF): `c99c0185aebe7058f018f4d236b4fd4b07190f54352bb99c62b784adc8777963`.

</details>
<!-- translation-metadata:end -->

[Українська](desktop-installation.uk.md).

On Windows 10/11 x64, open `/downloads`, create a personal account or sign in,
download the exe and open it. The Install and open button copies the application
to `%LOCALAPPDATA%/Programs/Lokvetia Core` and adds a Start menu shortcut.
Administrator privileges, Git and a separate Python installation are unnecessary.
This preview has no publisher code signature; its SHA-256 is published alongside it.

The application opens first-run setup in the browser. Data lives in
`%LOCALAPPDATA%/Lokvetia/workspace`, separately from the executable. Starting
again opens the existing studio. The quit button stops the local HTTP server.
The studio binds only to `127.0.0.1` and requires its own local session.

AI providers, local models and Godot are configured separately. Downloading Core
does not provide an AI subscription or establish complete autonomous game
creation. macOS/Linux packages and signed automatic updates are not included.

## Accounts

`LOKVETIA_PERSONAL_REGISTRATION=1` enables self-registration in the identity service.
Invitation-only registration remains the default. A personal account receives
an opaque identifier and access only to its own profile and downloads. It does
not join an organization or see shared projects, credentials, reports or studio
controls. Email is a login identifier; email ownership verification and email
password recovery are not implemented. A live invitation reserves its address
and cannot be replaced through self-registration. Promotion remains separate.

## Packaging and publication

On Windows with Core and its web dependencies installed:

```powershell
python -m pip install pyinstaller==6.22.3
python scripts/build_desktop.py --out desktop-output
```

The package includes the interpreter, code and dependencies, but no local data,
credentials, provider profiles or models. `desktop-release.json` records size
and SHA-256. Building does not mark the package tested: install and run that
exact exe in a separate empty directory and preserve the result.

`LOKVETIA_DESKTOP_RELEASE_DIR` points to a separate directory containing the
manifest and published artifacts. Mount it read-only into containers. Downloads
allow only manifest-listed names, verify size and SHA-256, and require sign-in.
Enable registration only after updating identity, Core, Lokiravia and the gateway:
every HTTP boundary must deny `account_user` access to operator data. The gateway
requires the separate `workspace_access` signal.
