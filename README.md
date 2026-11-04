# SWE Atlas

[![arXiv](https://img.shields.io/badge/arXiv-2605.08366-b31b1b.svg)](https://arxiv.org/abs/2605.08366)

SWE Atlas is a benchmark for evaluating AI coding agents across a spectrum of professional software engineering tasks. Rather than measuring a single skill in isolation, SWE Atlas consists of multiple leaderboards that target distinct and complementary capabilities in the Software Development Cycle.

This repository has the data and instructions for running [SWE Atlas - Codebase QnA](https://labs.scale.com/leaderboard/sweatlas-qna), [SWE Atlas - Test Writing](https://labs.scale.com/leaderboard/sweatlas-tw) and [SWE Atlas - Refactoring](https://labs.scale.com/leaderboard/sweatlas-refactoring).

| Lane | Tasks | The agent is asked to | Graded on |
|---|---|---|---|
| `qa-*` Codebase QnA | 124 | answer a question about a codebase | rubrics, rated by an LLM judge against the answer |
| `tw-*` Test Writing | 90 | write tests for described behaviour | the tests must pass on the code and fail once the implementation is replaced, plus rubrics and a test manifest check |
| `rf-*` Refactoring | 70 | refactor code without changing behaviour | the relevant tests must pass after the change, with no protected test file edited, plus rubrics |

```
data/<lane>-<id>/
├── task.toml          image, resources, timeouts, network allowlist, verifier settings
├── instruction.md     what the agent is given
├── environment/       Dockerfile: a single FROM of the task's digest-pinned image
├── solution/          the reference solution
└── tests/             the grader, rubrics and judge prompts
run_config/            job.yaml (the run config) and helper scripts
```

Every task runs on a prebuilt public image, pinned by `@sha256` digest in `docker_image` in its
`task.toml`. No registry credentials are needed.

## Requirements

[uv](https://docs.astral.sh/uv/getting-started/installation/),
[harbor](https://github.com/laude-institute/harbor) 0.24.0 or 0.25.0 with its Modal extra, and a
[Modal](https://modal.com/) account. The Modal CLI is installed separately:

```bash
uv tool install 'harbor[modal]==0.25.0'
uv tool install modal
modal setup
```

## Environment Variables

```bash
cp .env.example .env
```

Fill in `.env`. Harbor reads it through `--env-file .env`, and the scripts in `run_config/` read it
directly.

- **Agent.** claude-code reads `ANTHROPIC_API_KEY` (and `ANTHROPIC_BASE_URL` for a proxy); codex
  and mini-swe-agent read `OPENAI_API_KEY` and `OPENAI_BASE_URL`.
- **Judge.** Rubrics are rated by an LLM through any OpenAI-compatible endpoint, set with
  `OPENAI_API_KEY` and `OPENAI_API_BASE` (or `JUDGE_API_KEY` and `JUDGE_BASE_URL` to use a separate
  key). The judge is `openai/gpt-6.1-sol` at high reasoning effort, and each verdict is the majority
  of 3 votes; `JUDGE_MODEL`, `JUDGE_REASONING_EFFORT` and `JUDGE_VOTES` change them.

The agent runs under a network allowlist, so the hostname of its model API (no `https://`, no path)
has to be added: `extra_allowed_hosts` in `run_config/job.yaml`, or `--allow-agent-host <host>` on
a plain `harbor run`. Use an API key that serves only the model under evaluation, without web-search
models or tools.

## Check the setup

The `oracle` agent applies each task's reference solution and grades it. One task should score
`reward 1.0`:

```bash
harbor run -p data -a oracle -e modal --env-file .env -y -i qa-6905333b74f22949d97ba9cc
```

`bash run_config/oracle.sh` runs the reference solutions over all three lanes and ends with a
score table (`bash run_config/nop.sh` does the same with an agent that does nothing, which should
score 0).

## Run an agent

`run_config/job.yaml` runs one agent over all 284 tasks with 3 attempts per task (pass@3), and
retries infrastructure and agent-timeout failures. Set the model under evaluation and its API
hostname (the two lines marked `EDIT:`), then:

```bash
harbor run -c run_config/job.yaml --env-file .env -y

# one lane, or one task
harbor run -c run_config/job.yaml -p data --env-file .env -y -i 'rf-*'
harbor run -c run_config/job.yaml -p data --env-file .env -y -i rf-69391d8d1ce51c407be1e533
```

- **Other agents.** `job.yaml` has commented blocks for codex, mini-swe-agent, kimi-cli and vibe.
  mini-swe-agent runs one lane at a time with that lane's prompt config,
  `run_config/mswea/<lane>.yaml`. For any other harbor agent (`harbor run --help` lists them),
  replace the whole agent block: set `name`, `model_name`, `extra_allowed_hosts`, and under `kwargs`
  only the options that agent accepts (`harbor agent schema <name>` lists them). claude-code's
  `version` and `disallowed_tools` do not carry over, and not every agent accepts
  `reasoning_effort`. Turn off the agent's web tools, e.g. `disable_web_search: true` for opencode.
- **Agent versions.** `job.yaml` pins claude-code 2.1.293, codex 0.142.1, mini-swe-agent 2.4.6,
  kimi-cli 1.51.0 and vibe 3.0.4 through the agent's `version` kwarg.
- **Run settings.** `n_attempts` (pass@k), `n_concurrent_trials` and `retry.max_retries` are set in
  `job.yaml`.

## Results

Harbor writes each run to `jobs/<job-name>/`, one directory per trial. For pass@1 and pass@k:

```bash
python3 run_config/summary.py jobs/<job-name>
```

Trials that ended on an infrastructure or agent-timeout error, or that the grader marked as an
infrastructure failure, are left out of pass@1 and listed under the table; rerun them. Every other
trial counts as graded: a refusal, an empty answer or a submission the judge could not rate scores 0
(the `not graded` column counts these). An agent job with qa trials, other than oracle or nop, gets
a second row: qa without the 7 tasks some models refuse to answer. Browse a run with
`harbor view jobs`.

## License

Apache License 2.0, see [LICENSE](LICENSE). Some qa rubric criteria reuse wording from the
Artificial Analysis SWE-Atlas-QnA-AA dataset; see [NOTICE](NOTICE).

## Citation

If you use SWE Atlas in your research, please cite our paper:

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
