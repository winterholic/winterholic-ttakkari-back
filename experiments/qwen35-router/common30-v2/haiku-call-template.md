# Haiku paired-call template (prepared only; not executed)

The 30 frozen lines are in `hosted-comparison-inputs-v1.jsonl`. Each line already contains the identical v2 system text, serialized user content, empty candidate snapshot, strict JSON Schema, temperature/seed/output-token settings, and `repeat_count=3`.

For each line and each of three repeats, pass the `user` value as the sole query; use `system` as the exact replacement system prompt and the row's `response_schema` for structured output. A single request command is:

```sh
schema="$(cat experiments/qwen35-router/common30-v2/luna-output-schema.json)"
user_payload='... value of one row.user ...'
claude -p --model haiku --bare \
  --system-prompt-file experiments/qwen35-router/common30-v2/system-prompt-v2.txt \
  --tools "" --strict-mcp-config --mcp-config '{}' \
  --json-schema "$schema" --no-session-persistence --max-turns 1 \
  --output-format json "$user_payload"
```

Run only when a fresh Haiku cap is available and the authorized provider path is usable. Retain stdout, stderr, exit status, per-request wall time and server-reported usage. Parse the structured JSON and run the same Pydantic/schema/membership, gold exact/allowed-action, severity, coverage, repeat stability and latency analysis as the local models. Do not compare provider wall time to local model latency without accounting for the CLI wrapper and network path.

The installed snapshot said Claude was stale at 99% until 02:30 KST; that time was not fresh enough to justify a call in this run. Therefore Haiku remains unmeasured.

The flags and structured output behavior are documented in the [official Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage): `--system-prompt-file`, `--tools ""`, `--json-schema`, `--no-session-persistence`, `--max-turns`, and `--output-format json`.
