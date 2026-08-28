"""Check the history trimming keeps every transcript valid for the model.

Only Ollama's shape is covered now. The Anthropic content-block shape and
the _trim_history that handled it were removed with that backend.
"""
import sys
from pathlib import Path

# Relative, not hardcoded — see the note in test_event_loop.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import _trim_ollama_history, HISTORY_TURNS


def user(text):
    return {"role": "user", "content": text}


# --- Ollama shape ---
def o_assistant(content, calls=None):
    return {"role": "assistant", "content": content, "tool_calls": calls or []}


hist = []
for turn in range(20):
    msgs = list(hist) + [user(f"play {turn}")]
    msgs.append(o_assistant("", [{"function": {"name": "play_media", "arguments": {}}}]))
    msgs.append({"role": "tool", "content": "ok"})
    msgs.append(o_assistant(f"Playing {turn}"))
    hist = _trim_ollama_history(msgs)
    assert not hist or hist[0]["role"] == "user", hist[0]
    assert not hist or (hist[-1]["role"] == "assistant" and not hist[-1]["tool_calls"])
print(f"ollama rolling: OK, settled at {len(hist)} messages")

print("\nALL PASS")
