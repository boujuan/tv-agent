# Releasing

Each component is versioned and released on its own, from tags named `<component>-v<version>` (SemVer):

| Component | Version lives in | Tag | The release workflow publishes |
|---|---|---|---|
| server | `server/pyproject.toml` (`version`) | `server-vX.Y.Z` | wheel + sdist (`tv_agent-X.Y.Z…`) |
| panel | `panel/tvpanel/__init__.py` (`__version__`) | `panel-vX.Y.Z` | `tv-agent-panel-X.Y.Z.tar.gz` |
| android | `android/app/build.gradle` (`versionName`; also bump `versionCode`) | `android-vX.Y.Z` | `tv-agent-android-X.Y.Z.apk` |

Reserved for later: `web-vX.Y.Z` for a browser UI over the same local API.

## Cutting a release

```bash
# 1. bump the version where the table says, and add a "## <component> X.Y.Z — YYYY-MM-DD" section to CHANGELOG.md
#    (or give "## <component> — unreleased" that version and date)
git commit -am "server 0.2.0"
git tag -a server-v0.2.0 -m "server 0.2.0"
git push origin main server-v0.2.0
```

`.github/workflows/release.yml` then checks the tag against the version in the source, builds the artifacts,
adds `SHA256SUMS`, and creates the GitHub Release with that CHANGELOG section as its notes. CI
(`.github/workflows/ci.yml`) runs the server's tests, checks the panel and builds the APK on every push.

The components talk over the server's HTTP API (documented in `server/README.md`). When it changes
incompatibly, release the server first and say in the panel's and app's CHANGELOG sections which server version
they need.

## Channels (where releases can go next)

| Channel | Component | What's needed |
|---|---|---|
| AUR | server, panel | `packaging/aur/` has draft recipes. AUR packages must download their sources without login, so the repo (or its release assets) has to be public first. |
| PyPI | server | `uv publish` of the release's wheel/sdist (a PyPI token as a repo secret). |
| Flatpak / AppImage | panel | A manifest bundling PySide6; the panel then needs the server's API reachable from the sandbox (it already uses only HTTP, `systemctl --user` and `pw-record`). |
| Debian / Fedora | server, panel | nfpm or native packaging from the same artifacts. |
| macOS, Windows | panel | PyInstaller (or briefcase) builds; the services part needs launchd / Windows equivalents of `systemctl --user`, and the mic `pw-record`'s. |
| macOS, Windows | server | Replace the systemd units with launchd / a Windows service; sherpa-onnx and llama.cpp both support them. |
| Android (Play, F-Droid, Obtainium) | android | Releases are signed with one key (see below), so each installs over the last. GitHub Releases already work with Obtainium. F-Droid can ship these signed APKs if its build reproduces them (`AllowedAPKSigningKeys`: the certificate digest). Play: upload this key as the app signing key (Play App Signing), so Play and GitHub installs can update each other. |
| Web | new `web/` | A browser UI over the local API (needs CORS and a way to get the token to the browser). |

## Signing (Android)

Every release APK is signed with the same release key, so a newer one installs over an older one (and over a local
build signed with it). `release.yml` refuses to publish an APK signed with any other key.

- Key: PKCS12 keystore, alias `tv-agent`, RSA 4096, valid until 2054; one password for the keystore and the key.
- Certificate SHA-256: `bd934e07d6784766bbfb3948bf3467ef9b2cb0c9c1146fdf9528dc76c8eb1e08`.
- Backup: the maintainer's password manager (keystore file and password). Without the key there are no more
  updates: every install would have to be uninstalled and replaced.
- CI: repo secrets `ANDROID_KEYSTORE_BASE64` (the keystore, `base64 -w0`) and `ANDROID_KEYSTORE_PASSWORD`; only
  `release.yml` uses them.

`android/app/build.gradle` reads the key from Gradle properties, or else environment variables, of these names:

| Name | Value |
|---|---|
| `TV_AGENT_KEYSTORE` | Path to the keystore |
| `TV_AGENT_KEYSTORE_PASSWORD` | Its password |
| `TV_AGENT_KEY_ALIAS` | `tv-agent` |
| `TV_AGENT_KEY_PASSWORD` | Only if the key's password differs from the keystore's |

To sign local builds with it, copy the keystore from the backup into a private folder and add the names to
`~/.gradle/gradle.properties` (`chmod 600`):

```properties
TV_AGENT_KEYSTORE=/home/<you>/.local/share/tv-agent/signing/android-release.p12
TV_AGENT_KEYSTORE_PASSWORD=<password>
TV_AGENT_KEY_ALIAS=tv-agent
```

`android/build-and-install.sh` then prints that the APK is signed by `CN=tv-agent`. Neither file goes in the repo
(`.gitignore` skips `*.jks`, `*.keystore` and `*.p12`).

To set the repo secrets again (a new repo, a lost secret): `base64 -w0 android-release.p12 | gh secret set
ANDROID_KEYSTORE_BASE64`, then `gh secret set ANDROID_KEYSTORE_PASSWORD` (it asks for the value).

Without the key (CI on branches and pull requests, any other computer), release builds are signed with that
computer's debug key: they build and install, but can't update an install signed with the release key, nor the other
way round. To switch the TV from one to the other: `tv-agent tv shell pm uninstall com.boujuan.tvagent`, then
`tv-agent tv install <apk>` (which pairs again). Android 0.3.0 was signed with a debug key, so going from it to a
release-key build needs this once.
