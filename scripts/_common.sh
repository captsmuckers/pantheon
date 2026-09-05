# Shared helpers for the launcher scripts. Sourced, never executed.
#
# The PowerShell originals had no equivalent of this file because most of what
# they did was working around Windows: every one of them wrapped its payload in
# cmd.exe, because Python logs to stderr and PowerShell 5.1 turns a native
# stderr line into a terminating NativeCommandError. A shell redirects stderr
# without drama, so that entire layer is gone and what is left is small enough
# to share.
#
# WRITTEN FOR BASH 3.2, which is what macOS ships and what /usr/bin/env bash
# finds unless someone has installed a newer one. That rules out `mapfile` and
# makes `"${arr[@]}"` on an empty array an error under `set -u`, so pid lists
# below are plain whitespace-separated strings rather than arrays. They only
# ever hold digits, so the word splitting is safe and deliberate.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGDIR="$ROOT/logs"
KEEP=10

# Put Homebrew on PATH, because launchd will not.
#
# A LaunchAgent inherits none of your shell's environment: no profile is read,
# and PATH defaults to /usr/bin:/bin:/usr/sbin:/sbin, which does not include
# /opt/homebrew/bin. `launchctl getenv PATH` returns nothing at all here.
#
# That is not a theoretical tidy-up. It is why com.athena.bot crash-looped
# every 11 seconds while com.athena.tts ran fine: the TTS plist invokes its
# interpreter by absolute path and needs no lookup, whereas bot.py's preflight
# calls shutil.which("mpv") and reported "mpv was not found" on a machine where
# mpv is plainly installed. yt-dlp resolves the same way (config._find_ytdlp)
# and would have been the next thing to go missing.
#
# Set here rather than in the plists so it covers every launcher — interactive
# and launchd alike — from one place, and so a hand-run script behaves
# identically to a supervised one.
case ":$PATH:" in
    *":/opt/homebrew/bin:"*) ;;
    *) PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH" ;;
esac
export PATH

# A checkout's own .venv wins over whatever is on PATH. Voice needs numpy,
# sounddevice and faster-whisper, which are deliberately not installed globally
# so production keeps the dependency set it was tested against.
pick_python() {
    if [ -x "$ROOT/.venv/bin/python" ]; then
        echo "$ROOT/.venv/bin/python"
    elif command -v python3.13 >/dev/null 2>&1; then
        command -v python3.13
    else
        command -v python3
    fi
}

# One file per run, so two runs never contend for the same handle, and old ones
# are pruned. Same reasoning as the Windows version — an orphan holding the
# previous log open is exactly what once blocked a start from recording why it
# failed.
new_log() {
    prefix="$1"
    mkdir -p "$LOGDIR"
    # shellcheck disable=SC2012
    ls -t "$LOGDIR/$prefix-"*.log 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
        rm -f "$old"
    done
    echo "$LOGDIR/$prefix-$(date +%Y%m%d-%H%M%S).log"
}

# pgrep -f, as a whitespace-separated list, excluding this script's own process
# tree. Without that exclusion a stop script whose own command line contains the
# pattern matches itself, which is a memorable way to kill your own terminal.
find_pids() {
    # `|| true` is load-bearing under `set -o pipefail`: grep -v exits 1 when it
    # filters everything out, which is the ordinary "nothing is running" case,
    # and without this the start scripts' `set -e` would abort on it.
    pgrep -f "$1" 2>/dev/null | grep -v -e "^$$\$" -e "^$PPID\$" | tr '\n' ' ' | sed 's/ *$//' || true
}

# One value out of .env, without sourcing the whole file.
#
# Sourcing it would be shorter and wrong: .env holds tokens and a persona line
# full of quotes and apostrophes, and `source` would try to execute all of it.
# This reads exactly the key asked for and nothing else.
env_value() {
    local key="$1" default="${2:-}"
    local line
    line="$(grep -E "^${key}=" "$ROOT/.env" 2>/dev/null | tail -1)" || true
    [ -z "$line" ] && { echo "$default"; return; }
    local value="${line#*=}"
    [ -z "$value" ] && { echo "$default"; return; }
    # Unquote, the same way python-dotenv and gui/envfile.py do. The panel
    # quotes any value that needs it, and this returned the raw right-hand
    # side — so a quoted setting reached the service with its quotes still
    # attached. For TTS_VOICE_REF_TEXT that put a stray " at the front of the
    # reference transcript, which misaligns in-context cloning and shows up
    # as "the clone is worse" rather than as any kind of error.
    case "$value" in
        \"*\")
            value="${value#\"}"; value="${value%\"}"
            # Only the escapes dotenv honours inside double quotes.
            value="$(printf '%s' "$value" | sed -e 's/\\n/\
/g' -e 's/\\"/"/g' -e 's/\\\\/\\/g')"
            ;;
        \'*\')
            value="${value#\'}"; value="${value%\'}"
            ;;
    esac
    echo "$value"
}

# Which interpreter serves TTS depends on the engine, because the two cannot
# share one. Chatterbox pins torch 2.6 and Kokoro runs on 2.13, so each has its
# own venv and the server is started from whichever matches.
tts_python() {
    engine="$(env_value TTS_ENGINE kokoro)"
    # Qwen runs on MLX, which cannot share an environment with either of the
    # others: mlx-audio pulls no torch at all, and Chatterbox pins torch 2.6
    # against Kokoro's 2.14. Three engines, three venvs, one reason.
    if [ "$engine" = "qwen" ] && [ -x "$ROOT/tts/.venv-mlx/bin/python" ]; then
        echo "$ROOT/tts/.venv-mlx/bin/python"
    elif [ "$engine" = "chatterbox" ] \
       && [ -x "$ROOT/tts/.venv-chatterbox/bin/python" ]; then
        echo "$ROOT/tts/.venv-chatterbox/bin/python"
    elif [ -x "$ROOT/tts/.venv/bin/python" ]; then
        echo "$ROOT/tts/.venv/bin/python"
    else
        pick_python
    fi
}

# The flags to hand tts_server.py.
#
# --voice and --lang are always passed, because they decide which phonemiser is
# preloaded and what /health reports. Without them the server preloads British
# and the settings page shows British no matter what .env says — the reported
# state would be a guess rather than the truth.
tts_args() {
    local engine ref voice lang
    voice="$(env_value TTS_VOICE bf_emma)"
    lang="$(env_value TTS_LANG_CODE auto)"
    printf -- "--voice %s --lang %s" "$voice" "$lang"

    engine="$(env_value TTS_ENGINE kokoro)"
    [ "$engine" = "kokoro" ] && return
    printf -- " --engine %s" "$engine"
    ref="$(env_value TTS_VOICE_REF "")"
    if [ -n "$ref" ]; then
        case "$ref" in /*) ;; *) ref="$ROOT/$ref" ;; esac
        printf -- " --voice-ref %s" "$ref"
    fi
    [ "$engine" != "qwen" ] && return

    # Qwen's checkpoint IS its mode, so this always goes on the command line.
    printf -- " --qwen-model %s" \
        "$(env_value TTS_QWEN_MODEL mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit)"
    # NOT the free-text settings. tts_args' output is deliberately word-split
    # by its callers ("$PYTHON" tts_server.py $ENGINE_ARGS), so a voice design
    # like "a low, dry English woman" would arrive as eight arguments and
    # argparse would take the first word. They go through the environment
    # instead - see tts_exports.
    return 0
}

# The free-text TTS settings, exported rather than passed as arguments.
#
# Call this WITHOUT command substitution - `tts_exports`, never `$(tts_exports)`
# - or the exports land in a subshell and vanish. tts_server.py reads these as
# the defaults for --voice-design and --ref-text, so a hand-run server with
# explicit flags still wins.
tts_exports() {
    export ATHENA_TTS_VOICE_DESIGN="$(env_value TTS_VOICE_DESIGN "")"
    export ATHENA_TTS_REF_TEXT="$(env_value TTS_VOICE_REF_TEXT "")"
}

# Stop a process politely before forcing it.
#
# SIGINT rather than SIGTERM, and that is a real difference from the Windows
# script, which only ever had Stop-Process -Force. Python's default SIGTERM
# handler exits without unwinding, so bot.py's `finally: await
# player.shutdown()` never runs and mpv is left holding a fullscreen window.
# SIGINT raises KeyboardInterrupt, the finally block runs, and mpv exits on its
# own. SIGKILL stays as the fallback for a process that ignores it.
stop_pids() {
    label="$1"
    pids="$2"
    [ -z "$pids" ] && return 1
    kill -INT $pids 2>/dev/null || true
    i=0
    while [ $i -lt 20 ]; do
        sleep 0.25
        alive=""
        for p in $pids; do
            kill -0 "$p" 2>/dev/null && alive="$alive $p"
        done
        if [ -z "$alive" ]; then
            echo "stopped $label: $pids"
            return 0
        fi
        i=$((i + 1))
    done
    kill -9 $pids 2>/dev/null || true
    echo "force-killed $label: $pids"
    return 0
}
