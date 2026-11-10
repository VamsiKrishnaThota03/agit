# Design notes

Why agit is shaped the way it is, and what the ledger format actually guarantees.

## The one load-bearing decision

Every agent step commits a **full snapshot of the workspace** into the ledger
commit, next to the step's own metadata.

Snapshotting whole trees sounds wasteful until you notice what it buys. Git
already deduplicates unchanged blobs, so the marginal cost of a step is one
tree plus the blobs that actually changed. In exchange, every hard feature
becomes a git operation that already works and is already correct:

| feature | implementation |
|---|---|
| per-line attribution | `git blame` on `workspace/<file>`, then read the commit's trailers |
| rewind the workspace | check out that commit's `workspace/` tree |
| fork a run | `git checkout -B run/<new> <step-sha>` |
| compare two attempts | `git diff run/a run/b -- workspace/` |
| "what changed in step 7" | `git diff <sha>^ <sha> -- workspace/` |

The alternative — storing per-step patches in a database and reimplementing
blame over them — means owning rename detection, whitespace handling and merge
semantics forever. agit owns none of that.

## Layout

```
your-project/
├── .git/                     your repository, never touched by agit
├── .gitignore                agit appends `.agit/` on first run
└── .agit/
    ├── config.json           optional per-project settings
    ├── cassette.json         request-hash -> recorded model response
    ├── replays/              scratch workspaces for `agit replay`
    └── ledger/               a separate git repository
        ├── run.json          state of the run owning the current branch
        ├── steps/0005/
        │   ├── meta.json     structured record of the step
        │   ├── request.json  exact messages sent to the model
        │   ├── response.json exact response received
        │   ├── result.txt    tool output, as the model saw it
        │   └── verify.txt    verify command output, when it ran
        └── workspace/        snapshot of the project after this step
```

Branch per run (`run/<id>`), all branching off an empty `base` commit so any two
runs have a common ancestor and therefore a meaningful diff.

## Commit trailers

Structured data lives in git trailers, so `git log --format` and `git
interpret-trailers` can read a run without agit installed:

```
Agit-Run         run id this step belongs to
Agit-Step        step number (0 = baseline snapshot)
Agit-Kind        baseline | tool | finish | run-end
Agit-Tool        tool name
Agit-Target      file, command or pattern the tool acted on
Agit-Ok          did the tool succeed
Agit-Model       model that chose this step
Agit-Tokens-In   prompt tokens
Agit-Tokens-Out  completion tokens
Agit-Duration-Ms model latency
Agit-Added       lines added
Agit-Removed     lines removed
Agit-Verify      pass | fail, when the verify gate ran at this step
```

The commit *message body* is the model's own rationale, verbatim. That is
deliberate: the commit message is where engineers already look for "why".

## Determinism, and what breaks it

`agit replay` reproduces a run with no network. The cassette key is a hash of
the full request (model, temperature, messages, tool schemas), so replay only
works if the prompt is byte-identical. Two things had to change to make that
true, and both are improvements in their own right:

1. **No absolute paths in the prompt.** The system prompt says "paths are
   relative to the workspace root" and never names the root. Tool output is
   scrubbed of the workspace path and `$HOME` before the model sees it. Your
   directory layout is nobody else's business, including the provider's.
2. **A volatility mask on the cassette key.** Elapsed times (`0.004s`), clock
   times, pids, hex addresses, temp paths and epoch numbers are normalised
   *for hashing only*. The model still sees the real text; two otherwise
   identical runs just stop looking different because a test suite took one
   millisecond longer.

What still legitimately diverges: a different model, a different temperature, a
changed workspace, or a non-deterministic model at temperature 0. That is the
point of `agit replay --live`, which re-asks the model and reports the first
step where today's answer differs — a flakiness report for a system that has no
other way to express flakiness.

## The agent loop

```
system prompt (tools, file list, definition of done)
└─ user task
   └─ loop:
      model.complete(messages)        one tool call per turn
      ├─ no tool call?                nudge once, then stop as "stalled"
      ├─ execute tool                 confined to the workspace
      ├─ commit step                  rationale + snapshot + raw model I/O
      ├─ tool == finish?              run the verify command
      │   ├─ pass                     status completed + VERIFIED
      │   └─ fail                     feed the failure back, keep going
      └─ budget checks                max steps, max tokens
```

Older tool results are collapsed to one line each once there are more than
eight of them (`_compact`), which keeps a long run inside a sane context
without losing the recent observations the model is actually reasoning over.
The full, uncompacted message list is still written to `steps/NNNN/request.json`.

## Safety model

Stated plainly, because the gap between "safe" and "feels safe" is where
accidents live.

- Path confinement is enforced at `Toolbox.resolve`, before any file operation,
  and refuses `../`, absolute paths outside the root, and `.git` / `.agit`.
- `run_command` has two gates: a hard denylist that applies even with `--yes`
  (irreversible or outward-facing commands — `sudo`, `rm -rf /`, `git push`,
  `npm publish`, `terraform apply`, `curl … | sh`, …), and per-command human
  approval otherwise. A refusal is recorded as a step so the attempt is visible.
- agit never writes to your repository's git history, never pushes, never
  rewrites. The ledger is a different repository.
- Replay runs in a scratch directory and only re-runs commands the recorded run
  already ran.
- This is **not** a sandbox. Commands run as you. The denylist stops the
  obvious catastrophes; isolation needs a container, and agit does not pretend
  otherwise.

## Provider portability

One protocol: `POST {base_url}/chat/completions`, OpenAI-shaped, via
`urllib.request`. Presets exist for BharatCode, Ollama, Groq, OpenRouter,
Gemini and anything else compatible, all resolved from environment variables so
no credential ever lands in the repo. Retries back off on 429 and 5xx, and HTTP
errors are translated into advice (401 → check the key, 404 → check base URL or
model name, 400 → try `--no-native-tools`).

Models without tool calling are driven by a single-JSON-object protocol, with a
parser that tolerates code fences, surrounding prose and the four or five
different key names models like to invent (`tool`/`action`/`name`,
`args`/`arguments`/`parameters`).

## Zero dependencies, on purpose

A tool whose whole value proposition is "you will still be able to read this in
two years" has no business shipping a dependency tree that will not resolve in
two years. Standard library plus `git`. Installation is a clone.

## Testing strategy

The agent loop is the hard part to test, because it normally needs a model. The
`script` provider makes the model a pure function of the turn index: a JSON file
of tool calls, consulted deterministically. That means the full loop — tool
execution, ledger commits, the verify gate, budgets, forks, blame, replay — is
tested offline, with no key, in CI, in under a minute.

The scripted provider is honest about itself: it reports
`Agit-Model: agit-script/deterministic`, so a scripted run can never be mistaken
for a model run in the ledger.
