#!/usr/bin/env bash
# Download the local models into $MODELS_DIR (default /opt/reelvault/models). DESIGN-SAAS.md 3.17, 6.2, 6.4, 6.5.1.
# Run at image build time (the Dockerfile does it), so the server never downloads models at runtime.
#
#   MODELS_DIR=/opt/reelvault/models PYTHON=/opt/venv/bin/python deploy/fetch_models.sh
#   deploy/fetch_models.sh --no-whisper         # only CED-tiny + YuNet (small, for quick dev boxes)
#
# Layout (the contract other packages read through storage.models_dir()):
#   $MODELS_DIR/ced-tiny/model.int8.onnx, class_labels_indices.csv      sound tagger (Apache-2.0), 6.4
#   $MODELS_DIR/whisper/hub/models--Systran--faster-whisper-{small,base}  faster-whisper cache; HF_HOME points at
#                                                                           $MODELS_DIR/whisper, HF_HUB_OFFLINE=1
#   $MODELS_DIR/yunet/face_detection_yunet_2023mar.onnx                 face detector (MIT), 6.5.1
# Every file is checked against a pinned sha256 (or a pinned Hugging Face commit for whisper).
set -euo pipefail

MODELS_DIR="${MODELS_DIR:-/opt/reelvault/models}"
PYTHON="${PYTHON:-python3}"
WITH_WHISPER=1
[ "${1:-}" = "--no-whisper" ] && WITH_WHISPER=0

CED_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/sherpa-onnx-ced-tiny-audio-tagging-2024-04-19.tar.bz2"
CED_DIR_IN_TAR="sherpa-onnx-ced-tiny-audio-tagging-2024-04-19"
CED_MODEL_SHA="73aa22e783115f6a4ae169b36089907e07d0fd44795595eddea9c1bfc74cc945"
CED_LABELS_SHA="cdd1049833c4b86127c2773ac0d14a2754b6a6d0d1798002ed5c66e699708429"

YUNET_URLS=(
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
  "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"
)
YUNET_SHA="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

# name=commit of the Systran/faster-whisper-<name> repo on Hugging Face
WHISPER_MODELS=(
  "small=536b0662742c02347bc0e980a01041f333bce120"
  "base=ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
)

say() { printf '%s\n' "$*" >&2; }
sha_ok() { [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ]; }
fetch() {  # fetch URL DEST: retries, fails on HTTP errors
  curl -fsSL --retry 4 --retry-delay 3 --connect-timeout 20 -o "$2" "$1"
}

mkdir -p "$MODELS_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- CED-tiny (sound tagger)
CED="$MODELS_DIR/ced-tiny"
if [ -f "$CED/model.int8.onnx" ] && sha_ok "$CED/model.int8.onnx" "$CED_MODEL_SHA" \
   && [ -f "$CED/class_labels_indices.csv" ] && sha_ok "$CED/class_labels_indices.csv" "$CED_LABELS_SHA"; then
  say "CED-tiny: already there"
else
  say "CED-tiny: downloading"
  fetch "$CED_URL" "$TMP/ced.tar.bz2"
  tar -xjf "$TMP/ced.tar.bz2" -C "$TMP" "$CED_DIR_IN_TAR/model.int8.onnx" "$CED_DIR_IN_TAR/class_labels_indices.csv"
  sha_ok "$TMP/$CED_DIR_IN_TAR/model.int8.onnx" "$CED_MODEL_SHA" || { say "CED-tiny model checksum is wrong"; exit 1; }
  sha_ok "$TMP/$CED_DIR_IN_TAR/class_labels_indices.csv" "$CED_LABELS_SHA" || { say "CED-tiny labels checksum is wrong"; exit 1; }
  mkdir -p "$CED"
  mv "$TMP/$CED_DIR_IN_TAR/model.int8.onnx" "$TMP/$CED_DIR_IN_TAR/class_labels_indices.csv" "$CED/"
fi

# ---- YuNet (faces)
YN="$MODELS_DIR/yunet/face_detection_yunet_2023mar.onnx"
if [ -f "$YN" ] && sha_ok "$YN" "$YUNET_SHA"; then
  say "YuNet: already there"
else
  mkdir -p "$(dirname "$YN")"
  ok=0
  for u in "${YUNET_URLS[@]}"; do
    host="${u#https://}"; say "YuNet: downloading from ${host%%/*}"
    if fetch "$u" "$TMP/yunet.onnx" && sha_ok "$TMP/yunet.onnx" "$YUNET_SHA"; then ok=1; break; fi
  done
  [ "$ok" = 1 ] || { say "YuNet: download failed or checksum is wrong"; exit 1; }
  mv "$TMP/yunet.onnx" "$YN"
fi

# ---- faster-whisper small + base (Hugging Face cache layout, pinned commits)
if [ "$WITH_WHISPER" = 1 ]; then
  export HF_HOME="$MODELS_DIR/whisper"
  unset HF_HUB_OFFLINE
  for spec in "${WHISPER_MODELS[@]}"; do
    name="${spec%%=*}"; rev="${spec#*=}"
    say "whisper $name: checking"
    "$PYTHON" - "$name" "$rev" <<'PY'
import os, sys
from pathlib import Path
from faster_whisper import download_model
name, rev = sys.argv[1], sys.argv[2]
path = Path(download_model(name, revision=rev))
need = {"model.bin", "config.json", "tokenizer.json", "vocabulary.txt"}
have = {p.name for p in path.iterdir()}
missing = need - have
if missing:
    sys.exit(f"whisper {name}: missing files {sorted(missing)}")
if path.name != rev:
    sys.exit(f"whisper {name}: got snapshot {path.name}, expected {rev}")
# Make the offline lookup of the default revision ("main") find this pinned snapshot.
refs = path.parent.parent / "refs"
refs.mkdir(exist_ok=True)
(refs / "main").write_text(rev)
print(f"whisper {name}: ok ({sum(p.stat().st_size for p in path.iterdir()) // 1024 // 1024} MB)", file=sys.stderr)
PY
  done
  # Offline smoke test: load from the cache exactly the way the server will.
  HF_HUB_OFFLINE=1 "$PYTHON" -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')" \
    || { say "whisper: offline load test failed"; exit 1; }
fi

chmod -R a+rX "$MODELS_DIR"
say "Models are ready in $MODELS_DIR"
du -sh "$MODELS_DIR" >&2 || true
