# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's **Security → Report a vulnerability** flow for this repository. Do not open a public issue containing credentials, prompts, transcripts, private source code, or full API error payloads.

Include a minimal reproduction, affected version or commit, and the expected safety boundary.

## Security model

`claude-auto-retry` runs locally with the same operating-system permissions as the invoking user. It does not elevate Claude Code permissions and never adds `--dangerously-skip-permissions` automatically.

Interactive recovery uses tmux input injection because Claude Code does not expose an official API for submitting a new interactive turn. A recovery continuation may send Enter up to three times when no `UserPromptSubmit` Hook is observed: the initial submission, a 250-millisecond retry, and a final retry five seconds after the initial submission. Each retry rechecks provenance, pause/cancel state, and the target pane identity, but those checks cannot make tmux input injection transactional or provide exactly-once guarantees. Keep human supervision for tasks involving destructive or externally visible side effects.
