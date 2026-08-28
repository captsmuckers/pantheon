#!/usr/bin/env bash
#
# Turn any audio into a Chatterbox voice reference.
#
#   scripts/make-voice-ref.sh <input> <output.wav> [start-seconds]
#
# Chatterbox has no voice packs — it clones zero-shot from whatever clip you
# give it. What it actually reads is narrower than people assume, and this
# script exists because of it: the speaker encoder sees only the first 6
# seconds and the decoder only the first 10, measured from the model's own
# ENC_COND_LEN and DEC_COND_LEN. Everything past that is discarded.
#
# So a reference should be the BEST ten seconds, not the longest clip you have.
# Feed it a two-minute recording and it hears whatever happens to be at the
# front — which is usually a breath, a room tone, or someone saying "um".
#
# The third argument skips into the file if the good part starts later.
#
# What makes a reference work, in rough order of how much it matters:
#   - one speaker, no music, no other voices
#   - continuous speech, not a sentence with long gaps
#   - the tone you want back. A cheerful reference gives a cheerful clone, so
#     for a deadpan character read the line deadpan.
#   - clean recording. Compression artefacts and reverb get cloned too.
#
# Format does not matter: it is resampled to 24kHz internally, so anything
# librosa reads (wav, mp3, m4a, flac, aiff) is fine.
set -uo pipefail

IN="${1:-}"; OUT="${2:-}"; START="${3:-0}"
if [[ -z "$IN" || -z "$OUT" ]]; then
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
fi
if [[ ! -f "$IN" ]]; then
    echo "no such file: $IN" >&2
    exit 1
fi

# 10s from START, mono, 24kHz, loudness-normalised. Normalising matters more
# than it looks: a quiet reference produces a clone that mumbles.
ffmpeg -v error -y -ss "$START" -t 10 -i "$IN" \
    -af "loudnorm=I=-18:TP=-2:LRA=11" \
    -ar 24000 -ac 1 -c:a pcm_s16le "$OUT" || exit 1

DUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT")
printf "wrote %s  (%.1fs, mono, 24kHz)\n" "$OUT" "$DUR"
awk -v d="$DUR" 'BEGIN{
    if (d < 5) print "  WARNING: under 5s. The encoder wants ~6s; short references clone poorly."
    else if (d < 9.5) print "  note: under 10s, so the decoder sees all of it. Fine, just not maximal."
}'
