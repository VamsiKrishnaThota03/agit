# agit in GitHub Actions

Run an agent in CI and get back something you can review: a verified outcome,
per-line provenance, and an HTML dashboard attached to the job.

```yaml
- uses: VamsiKrishnaThota03/agit@main
  with:
    task: "add retry with exponential backoff to fetch()"
    verify: "pytest -q"
    provider: bharatcode
    base-url: ${{ secrets.BHARATCODE_BASE_URL }}
    api-key: ${{ secrets.BHARATCODE_API_KEY }}
```

The job fails unless the verify command passes, so an agent cannot merge its own
optimism. Every run uploads an `agit-run-<id>` artifact containing
`agit-dashboard.html`, `agit-run.html` and `agit-stats.json`.

## Inputs

| input | default | notes |
|---|---|---|
| `task` | — | required; what the agent should do |
| `verify` | `""` | command that must exit 0. **Set this.** Without it "done" is the model's own claim |
| `provider` | `bharatcode` | `ollama`, `groq`, `openrouter`, `gemini`, `openai-compatible`, `script` |
| `model` | provider default | |
| `base-url` | `""` | API base ending in `/v1` |
| `api-key` | `""` | pass a secret, never a literal |
| `max-steps` | `24` | hard ceiling on agent steps |
| `budget-tokens` | `250000` | hard ceiling on tokens |
| `working-directory` | `.` | the only directory the agent may edit |
| `comment` | `false` | comment the provenance summary on the PR |
| `fail-on-unverified` | `true` | fail the job when `verify` was given and did not pass |

## Outputs

| output | example |
|---|---|
| `run-id` | `1110-142806-f4db` |
| `verified` | `true` |
| `agent-share` | `73.9%` |
| `summary` | the agent's own description of what it changed |
| `stats-json` | path to the full machine-readable stats |

## Recipe: fix an issue when it is labelled

```yaml
name: agit
on:
  issues:
    types: [labeled]

permissions:
  contents: write
  pull-requests: write

jobs:
  fix:
    if: github.event.label.name == 'agit'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: VamsiKrishnaThota03/agit@main
        id: agit
        with:
          task: ${{ github.event.issue.title }}
          verify: "pytest -q"
          provider: bharatcode
          base-url: ${{ secrets.BHARATCODE_BASE_URL }}
          api-key: ${{ secrets.BHARATCODE_API_KEY }}

      - uses: peter-evans/create-pull-request@v6
        with:
          branch: agit/issue-${{ github.event.issue.number }}
          title: "${{ github.event.issue.title }}"
          body: |
            Closes #${{ github.event.issue.number }}

            Written by an agit run (`${{ steps.agit.outputs.run-id }}`),
            verified: ${{ steps.agit.outputs.verified }},
            agent-written lines: ${{ steps.agit.outputs.agent-share }}.

            > ${{ steps.agit.outputs.summary }}

            Per-line reasoning is in the `agit-run` artifact on the job.
```

The task here comes from an issue title — untrusted text written by whoever
opened the issue. The action only ever passes inputs to bash as environment
variables, never interpolated into a shell line, so a title containing
`"; rm -rf /` is just a weird task string. Keep it that way in your own steps:
use `env:` and `"$VAR"`, not `${{ }}` inside `run:`.

## Recipe: provenance comment on a pull request

```yaml
- uses: VamsiKrishnaThota03/agit@main
  with:
    task: "make the failing tests on this branch pass without changing the test files"
    verify: "pytest -q"
    comment: "true"          # needs permissions: pull-requests: write
    provider: groq
    api-key: ${{ secrets.GROQ_API_KEY }}
```

The comment carries the verdict, the agent's share of the lines it touched,
a per-file table, and the commands a reviewer can run locally to ask *why* about
any line.

## Guardrails worth knowing

- The agent runs with `--yes`, so it may run shell commands without asking.
  CI is the right place for that; your laptop is not. The hard denylist still
  applies: no `sudo`, no `rm -rf /`, no `git push`, no `npm publish`, no
  `terraform apply`, no piping the internet into a shell.
- Writes stay inside `working-directory`. `.git` and `.agit` are refused.
- Nothing is pushed or merged by agit itself. Opening a PR is a separate,
  explicit step that you control.
- Pin the action for reproducibility: `VamsiKrishnaThota03/agit@<commit-sha>`
  rather than `@main`.

## Without an API key

`provider: script` runs agit's deterministic offline planner: a read-only
survey, no model, no spend. Useful for testing the wiring of your workflow
before you add a secret — it is exactly what `action-selftest.yml` in this
repository does on every push.
