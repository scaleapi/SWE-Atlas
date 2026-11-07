# SWE Atlas v1.1

**Evaluating coding agents on professional software engineering work beyond simple issue resolution**

[![arXiv](https://img.shields.io/badge/arXiv-2605.08366-b31b1b.svg)](https://arxiv.org/abs/2605.08366)

SWE Atlas is a benchmark for coding agents across professional software engineering work: Codebase
QnA, Test Writing and Refactoring. This repository has the tasks and the run setup; results are on
the [SWE Atlas leaderboard](https://labs.scale.com/leaderboard/swe-atlas).

> **Update November 2026.** SWE Atlas v1.1 is live. It combines the three category leaderboards
> into a single, easy-to-run index covering the same 284 tasks as v1.0. Every model was re-run with
> a new judge (GPT-6.1 Sol, majority of 3 votes), refined grading criteria and restricted network
> access, so v1.1 scores aren't directly comparable to v1.0.

| Lane | Folder | Tasks | The agent is asked to | Graded on |
|---|---|---|---|---|
| Codebase QnA | `data/qa/` | 124 | answer a question about a codebase | rubrics, rated by an LLM judge against the answer |
| Test Writing | `data/tw/` | 90 | write tests for described behaviour | the tests must pass on the code and fail once the implementation is replaced, plus rubrics |
| Refactoring | `data/rf/` | 70 | refactor code without changing behaviour | the relevant tests must still pass, plus rubrics |

```
data/
├── qa/qa-<id>/          124 Codebase QnA tasks
├── rf/rf-<id>/          70 Refactoring tasks
└── tw/tw-<id>/          90 Test Writing tasks
    ├── task.toml        image, resources, timeouts, network allowlist
    ├── instruction.md   what the agent is given
    ├── environment/     Dockerfile: one FROM of a public, digest-pinned image
    ├── solution/        the reference solution
    └── tests/           the grader, rubrics and judge prompts
configs/                 one run config per run: oracle, nop and Anthropic and OpenAI leaderboard models
datasets/                harbor dataset manifests, one per lane
run.sh                   the entry point: ./run.sh <config.yaml> [qa|rf|tw] [harbor args]
```

## 1. Install

[uv](https://docs.astral.sh/uv/getting-started/installation/), harbor 0.24.0 or 0.25.0 with its
Modal extra, and a [Modal](https://modal.com/) account:

```bash
uv tool install 'harbor[modal]==0.25.0'
uv tool install modal
modal setup
```

## 2. Credentials

```bash
cp .env.example .env
```

- **Agent.** claude-code reads `ANTHROPIC_API_KEY`, codex `OPENAI_API_KEY`; each config names its
  keys, and `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` route through a proxy.
- **Judge.** Any OpenAI-compatible endpoint, through `OPENAI_API_KEY` and `OPENAI_API_BASE` (or
  `JUDGE_API_KEY` and `JUDGE_BASE_URL` for a separate key). The judge is `openai/gpt-6.1-sol` at
  high reasoning effort, majority of 3 votes; `JUDGE_MODEL`, `JUDGE_REASONING_EFFORT` and
  `JUDGE_VOTES` change it.

## 3. Check the setup

The `oracle` agent applies each task's reference solution and grades it. One task should score
`reward 1.0`:

```bash
./run.sh configs/oracle.yaml qa -i qa-6905333b74f22949d97ba9cc
```

`./run.sh configs/oracle.yaml` runs all 284 reference solutions; `./run.sh configs/nop.yaml` runs
an agent that does nothing, which should score 0.

## 4. Evaluate an agent

The Anthropic and OpenAI leaderboard models each have a config with the settings they were scored
with (harness, version, reasoning effort, web tools off, 3 attempts per task):

| Config | Harness |
|---|---|
| `configs/claude-opus-5-5.yaml`, `configs/claude-sonnet-5-5.yaml` | claude-code 2.1.293, effort max |
| `configs/gpt-6.1-sol.yaml`, `configs/gpt-6-astra.yaml` | codex 0.142.1, effort max |

```bash
./run.sh configs/claude-opus-5-5.yaml
```

- **Network.** The agent runs under an allowlist, so its model API hostname (no `https://`, no
  path) must be in `extra_allowed_hosts`; through a proxy, put the proxy's hostname there. Use a
  key that serves only the model under evaluation.
- **Web tools.** Keep them off: `disallowed_tools: WebSearch,WebFetch` for claude-code,
  `web_search: disabled` for codex.
- **Your own model or agent.** Copy the closest config and change `name`, `model_name`,
  `extra_allowed_hosts` and `kwargs` (`harbor agent schema <name>` lists an agent's options).
- **Run shape.** `n_attempts` (pass@k), `n_concurrent_trials` and `retry` are set per config.

## 5. Results

Harbor writes each run to `jobs/<job>/`, one directory per trial, and prints the mean of each
metric when the run ends (also in `jobs/<job>/result.json`). The score is `reward` (0 or 1 per
trial); its mean over all trials is pass@1 averaged over the attempts. Each trial's
`verifier/reward.json` has the metrics behind it. Browse a run with `harbor view jobs`.

An agent timeout is graded on the work the agent left; a refusal, an empty answer or a submission
the judge could not rate scores 0. Trials that ended on an infrastructure error are not scores:
rerun them in place with `harbor jobs resume -p jobs/<job> -f <ErrorType>`.

## 6. Part of the dataset

Without a lane, a config runs all three folders. Name a lane to run one folder; within a lane,
`-i` and `-x` take glob patterns over task names and `-l` caps the count:

```bash
./run.sh configs/gpt-6.1-sol.yaml rf                   # one lane: data/rf
./run.sh configs/gpt-6.1-sol.yaml qa -l 20             # 20 qa tasks
./run.sh configs/gpt-6.1-sol.yaml tw -x 'tw-6902*'     # tw without matching tasks
```

## Acknowledgements

Thanks to Artificial Analysis and Inductive ML for their contributions to the v1.1 release.
Artificial Analysis's SWE-Atlas-QnA-AA dataset provided wording for several updated Codebase QnA
rubric criteria, and Inductive ML's reward-hacking feedback further hardened the task environments.

## License

Apache License 2.0, see [LICENSE](LICENSE). Some qa rubric criteria reuse wording from the
Artificial Analysis SWE-Atlas-QnA-AA dataset; see [NOTICE](NOTICE).

## Citation

```bibtex
@misc{raghavendra2026sweatlasbenchmarkingcoding,
      title={SWE Atlas: Benchmarking Coding Agents Beyond Issue Resolution},
      author={Mohit Raghavendra and Soham Dan and Miguel Romero Calvo and Yannis Yiming He and Johannes Baptist Mols and Gautam Anand and Cole McCollum and Edgar Arakelyan and Vijay Bharadwaj and Andrew Park and Jeff Da and MohammadHossein Rezaei and Bing Liu and Brad Kenstler and Yunzhong He},
      year={2026},
      eprint={2605.08366},
      archivePrefix={arXiv},
      primaryClass={cs.LG},
      url={https://arxiv.org/abs/2605.08366},
}
```
