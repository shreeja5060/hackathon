# Shared

Common code and prompt fragments used by more than one phase — e.g. the
Anthropic client setup, a shared `config.py` for constants (model name, chunk
size, paths), and any prompt snippets reused across agents and the chat box.

Keep this small. If something is only used by one phase, it belongs in that
phase's folder instead.
