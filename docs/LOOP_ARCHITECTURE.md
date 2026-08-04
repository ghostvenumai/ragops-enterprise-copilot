# Controlled Codex Loop Architecture

The loop controller lives in `loop/controller.py`.

## Safety Model

- Reads `AGENTS.md`, `SPEC.md`, `TASKS.yaml`, and `.loop/last_result.json`.
- Selects exactly one eligible task.
- Builds a fixed command list:

```bash
codex exec --sandbox workspace-write --ask-for-approval never "<task>"
```

- Runs fixed quality and review commands after Codex.
- Never parses model output as shell code.
- Writes `.loop/state.json`, `.loop/history.jsonl`, `.loop/current_task.json`,
  `.loop/last_result.json`, and `.loop/blockers.json`.
- Uses atomic JSON writes.
- Stops on configured failure, no-progress, iteration, or security limits.
