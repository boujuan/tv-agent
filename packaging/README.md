# Packaging

Recipes for distribution channels, built from the GitHub Releases that the component tags produce (see
[RELEASING.md](../RELEASING.md)).

| Path | Status |
|---|---|
| `aur/tv-agent-panel/PKGBUILD` | Draft; installs the panel from `panel-v*`'s tarball (`/usr/bin/tv-agent-panel`, desktop entry, icon) |
| `aur/tv-agent/PKGBUILD` | Draft, not built yet; the open points are listed at its top |

To try a recipe locally: copy the folder, run `updpkgsums` and `makepkg -si` (needs the release assets to be
downloadable, i.e. a public repo, or a local `source=` file).
