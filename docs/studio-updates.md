# Check and download updates from Studio

In **Studio → Studio updates**, click **Check for updates**. The panel reports the installed version, the latest published Windows installer, its size and the check time. **Download Windows EXE** downloads the official release asset. Finish active work, close Studio, then open the downloaded installer and choose **Install and open**. Games and models do not need to be removed. Checking and downloading do not execute the installer or restart the application.

The desktop build includes `lokvetia-build.json` with its version and source commit. Source checkouts are identified as running from source; older EXEs without this metadata have an unknown version. A newer installed build is not offered an older installer as an update. Installers predating this UI need one manual upgrade to gain the check button.

The read-only endpoints are `/api/studio/updates` (local build information only) and `/api/studio/updates/check` (explicit network check). Both require authenticated read access. Navigating or polling Studio does not trigger network update checks and never uses AI. Successful checks are cached for 60 seconds; failures for three seconds. On network failure or rate limiting the panel offers retry and a link to the official release page, without retaining a stale download button.

The channel is the public `HappyMiha/Lokvetia-Core` GitHub Releases list, including current preview releases. Only non-draft releases with uploaded Windows EXE and `desktop-release.json` assets are considered. The manifest must match the version, exact filename, asset size and SHA-256 format and contain recorded testing evidence. URLs are constructed for this repository; redirects are limited to HTTPS GitHub asset hosts. Checks send no account credentials, project data, provider keys or inherited proxy credentials. Metadata reads have response-size and socket-time limits.

SHA-256 is displayed for manual file integrity checks; the browser download is not automatically hashed by Studio and the checksum is not a publisher signature. Existing preview EXEs remain unsigned. This is a check/download journey, not an unattended or signed automatic updater. New releases must be built from committed source, smoke-tested as the exact EXE, and published with the manifest and checksum file before they appear.

## Українською

Відкрийте **Студія → Оновлення студії → Перевірити оновлення**, потім **Завантажити EXE для Windows**. Якщо доступна нова версія, після завантаження завершіть роботу студії та встановіть її поверх наявної. Ігри й моделі залишаються на ПК. Для перевірки й завантаження з GitHub ключі або окремий вхід не потрібні.
