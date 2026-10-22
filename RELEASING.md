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
| Android (Play, F-Droid, Obtainium) | android | A release signing key: generate one, store it as repo secrets (keystore base64 + passwords), sign in `release.yml`. GitHub Releases already work with Obtainium. |
| Web | new `web/` | A browser UI over the local API (needs CORS and a way to get the token to the browser). |

## Signing (Android)

APKs are signed with the building machine's debug key. A local build (`android/build-and-install.sh`) installs over
what's on the TV; an APK from CI has the runner's key, so uninstall the old app first (`tv-agent tv shell pm
uninstall com.boujuan.tvagent`), install, and pair again. A shared release key in repo secrets removes that
step.
