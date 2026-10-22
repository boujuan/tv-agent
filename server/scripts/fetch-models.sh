#!/usr/bin/env bash
# Download llama.cpp (Vulkan build) and the models tv-agent uses, each checked against its published SHA-256,
# into ~/.local/opt and ~/.local/share/tv-agent/models (~2.2 GB). Safe to run again: verified files are kept.
set -euo pipefail
M=$HOME/.local/share/tv-agent/models
mkdir -p "$M" "$HOME/.local/opt"
get() {  # url dest sha256
  local url=$1 dest=$2 sha=$3
  if [ -f "$dest" ] && echo "$sha  $dest" | sha256sum -c --quiet 2>/dev/null; then echo "ok (cached) $dest"; return; fi
  curl -fL --retry 3 -sS -o "$dest.part" "$url"
  echo "$sha  $dest.part" | sha256sum -c --quiet && mv "$dest.part" "$dest" && echo "ok $dest ($(du -h "$dest" | cut -f1))"
}
mkdir -p "$M/d1-3B" "$M/parakeet-v2" "$HOME/.local/opt/llama.cpp-dl"
get https://github.com/ggml-org/llama.cpp/releases/download/b11804/llama-b11804-bin-ubuntu-vulkan-x64.tar.gz \
    "$HOME/.local/opt/llama.cpp-dl/llama-b11804-bin-ubuntu-vulkan-x64.tar.gz" d7805abfa078792f8f48b191047a7e392412cfa3279e1668f04d8cb14ec1d764
get https://huggingface.co/LiquidAI/d1-3B-GGUF/resolve/main/d1-3B-Q4_K_M.gguf \
    "$M/d1-3B/d1-3B-Q4_K_M.gguf" 16aff27ea2eefdc32b9897f43854a5d3170c1dc8dccb9c756905af30a4e22402
P="$M/parakeet-v2"
PARAKEET=sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8
if [ ! -d "$P/$PARAKEET" ]; then   # the archive is deleted once unpacked
  PSHA=$(curl -fsS https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/tags/asr-models | python3 -I -c '
import json, sys
assets = json.load(sys.stdin)["assets"]
print(next(a.get("digest") or "" for a in assets if a["name"] == sys.argv[1] + ".tar.bz2").removeprefix("sha256:"))' "$PARAKEET")
  [ -n "$PSHA" ] || { echo "no published digest for the Parakeet archive"; exit 1; }
  get "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$PARAKEET.tar.bz2" "$P/$PARAKEET.tar.bz2" "$PSHA"
  tar -xjf "$P/$PARAKEET.tar.bz2" -C "$P" && rm "$P/$PARAKEET.tar.bz2"
fi
B="$HOME/.local/opt/llama.cpp/b11804"
[ -x "$B/llama-b11804/llama-server" ] || { mkdir -p "$B" && tar -xzf "$HOME/.local/opt/llama.cpp-dl/llama-b11804-bin-ubuntu-vulkan-x64.tar.gz" -C "$B"; }
echo "Done: llama.cpp in $B, models in $M"
