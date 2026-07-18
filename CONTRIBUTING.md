# Contributing

Contributions are welcome. Please keep changes focused and preserve the safety properties documented in `README.md`.

## Development

Requirements:

- Python 3.9+
- tmux for manual interactive smoke tests

Run the automated suite:

```bash
python3 -m py_compile src/claude_auto.py
python3 -m unittest discover -s tests -t . -v
```

The installer tests run in an isolated temporary `HOME` with fake Claude Code and tmux executables. They must not access a real Claude session or modify the developer's settings.

## Pull requests

A pull request should include:

- Tests for behavior changes.
- Documentation updates for user-facing changes.
- No prompts, responses, API credentials, full error payloads, or machine-specific absolute paths.
- A clear explanation of any change to retry classification, tmux input handling, session locking, or side-effect safety.

Do not broaden error matching to every HTTP 422 or add arbitrary regular-expression matching without a safety review.
