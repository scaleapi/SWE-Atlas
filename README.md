# SWE Atlas

[![arXiv](https://img.shields.io/badge/arXiv-2605.08366-b31b1b.svg)](https://arxiv.org/abs/2605.08366)

SWE Atlas is a benchmark for evaluating AI coding agents across a spectrum of professional software engineering tasks. Rather than measuring a single skill in isolation, SWE Atlas consists of multiple leaderboards that target distinct and complementary capabilities in the Software Development Cycle. 

This repository has the data and instructions on running [SWE Atlas - Codebase QnA](https://labs.scale.com/leaderboard/sweatlas-qna), [SWE Atlas - Test Writing](https://labs.scale.com/leaderboard/sweatlas-tw) and [SWE Atlas - Refactoring](https://labs.scale.com/leaderboard/sweatlas-refactoring) 

> **UPDATE:** The SWE Atlas Test Writing (TW) and Refactoring (RF) tasks now run with internet access disabled — agents can reach only an allowlist of select domains (package registries and language toolchains) required to build and run tests, so reference solutions cannot be looked up online.
>
> Internet access is also restricted at the harness level — see `run_config/pass_k.sh` (e.g. `disallowed_tools=WebSearch,WebFetch` for Claude Code).

## Requirements

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then
[harbor](https://github.com/laude-institute/harbor) with its Modal extra. The extra is required:
a plain `uv tool install .` fails at run time with `MissingExtraError`.
```bash
git clone --branch v0.22.0 --depth 1 https://github.com/laude-institute/harbor.git
cd harbor
uv tool install '.[modal]'
```

### Pinned versions

The benchmark is validated on these versions. Harbor installs each agent inside the sandbox at run
time and takes the latest release unless `--ak version=<x>` is passed, so `run_config/pass_k.sh`
pins them.

| Component | Version | Where it's pinned |
|---|---|---|
| harbor | 0.22.0 | the install command above |
| claude-code | 2.1.293 | `CLAUDE_CODE_VERSION` in `run_config/pass_k.sh` |
| codex | 0.142.1 | `CODEX_VERSION` in `run_config/pass_k.sh` |
| mini-swe-agent | 2.4.6 | `MINI_SWE_AGENT_VERSION` in `run_config/pass_k.sh` |

Each trial records the installed agent version in `agent/trajectory.json`. For mini-swe-agent,
harbor 0.22.0 doesn't write that file, so read the version from the
`uv tool install mini-swe-agent==<version>` line in the trial's `trial.log`.

Set up [Modal](https://modal.com/) for sandbox environments. Harbor's extra doesn't put the Modal
CLI on your PATH, so install it separately:
```bash
uv tool install modal
modal setup
```

## Environment Variables

Create a .env with the following in the root of the repository:

```bash
# Agent under evaluation. claude-code reads the Anthropic variables; codex and mini-swe-agent read
# the OpenAI ones.
export ANTHROPIC_API_KEY=<your-anthropic-api-key>
export ANTHROPIC_BASE_URL=<optional, e.g. a LiteLLM proxy>
export OPENAI_BASE_URL=<optional, for codex and mini-swe-agent>
```

When `ANTHROPIC_BASE_URL` is set, harbor passes `MODEL` to claude-code unchanged. Use the model id
your proxy's Anthropic route expects, which is often without the `anthropic/` prefix
(`MODEL=claude-opus-4-6`).

To prevent solution lookup, the Test Writing and Refactoring tasks restrict network access during `agent.run()`.

Set `HARBOR_AGENT_ALLOWED_HOST` to the hostname of the API endpoint used by the agent under evaluation.
Enter only the hostname, without `https://` or a path. `run_config/pass_k.sh` passes it to Harbor through
`--allow-agent-host` for those two lanes. If either lane is requested without it, the script exits
before running any lane.

Examples include `api.openai.com`, `api.anthropic.com`, or the hostname of your LiteLLM proxy. Harbor adds
this hostname to the task's agent-phase allowlist; hosts outside the allowlist remain blocked.

```bash
export HARBOR_AGENT_ALLOWED_HOST=<your-agent-api-hostname>
```

LLM Judge (any OpenAI-compatible endpoint). We use Claude Opus 4.5 as the Judge model for rubric grading.
You can set the following credentials to access the Judge model.

```bash
export OPENAI_API_KEY=<your-judge-api-key>
export OPENAI_API_BASE=<your-judge-base-url>  # e.g. https://api.openai.com/v1
export EVAL_MODEL=<judge-model>               # optional; defaults to anthropic/claude-opus-4-5-20251101
```

codex and mini-swe-agent also read `OPENAI_API_KEY`, so with those agents one key serves both the
agent and the judge. The agents read their endpoint from `OPENAI_BASE_URL`, and the judge reads its
endpoint from `OPENAI_API_BASE`. They can be the same URL.

## Images

Every task runs on a prebuilt image: `docker_image` in `task.toml`, pinned by digest, and an
`environment/Dockerfile` that is only `FROM` that image. The images already contain everything
harbor's agent setup needs (claude-code, codex and mini-swe-agent), plus each task's own build
steps, so harbor runs them with no `--ek` arguments. The images are public, in
`ghcr.io/scaleapi/swe-atlas-v1.1`.

## Running

All the data is available in `data/qa` for Codebase QnA, `data/tw` for Test Writing and `data/rf` for Refactoring.

Two scripts in `run_config/` cover every agent and lane. Both run the lanes you name (default:
`qa rf tw`, one after another), and pass anything after `--` to every `harbor run`. Each lane is
its own harbor dataset, so `-i`/`-x` task filters apply within a lane, and task ids that aren't in
a lane are ignored.

Try one task first to check your setup. The oracle should score `reward 1.0`:

```bash
bash run_config/oracle.sh qa -- -i task-6905333b74f22949d97ba9cc
```

```bash
# pass@k for one agent
bash run_config/pass_k.sh claude-code                    # all three lanes
bash run_config/pass_k.sh mini-swe-agent tw
MODEL=openai/gpt-5.4 bash run_config/pass_k.sh codex rf tw
MODEL=anthropic/claude-opus-4-8 EFFORT=xhigh N=32 bash run_config/pass_k.sh claude-code rf -- \
  --max-retries 3 --agent-timeout-multiplier 3 --ae CLAUDE_ENABLE_STREAM_WATCHDOG=0

# reference solutions
bash run_config/oracle.sh                                # all three lanes
bash run_config/oracle.sh rf -- -i task-69391d8d1ce51c407be1e533
```

| Variable | Default |
|---|---|
| `MODEL` | `anthropic/claude-opus-4-6` (claude-code), `openai/anthropic/claude-opus-4-6` (mini-swe-agent); required for codex |
| `K` | 3 attempts per task (pass@k only; the oracle runs once) |
| `EFFORT` | `high` (`--ak reasoning_effort`) |
| `N` | concurrent trials: 24 claude-code, 16 codex and mini-swe-agent, 48 oracle |
| `JOB_NAME`, `RESULTS_DIR` | `<model>_<agent>_<effort>` (`oracle` for the oracle), `results/` (results go in `results/<lane>/`) |
| `DRY_RUN=1` | print the `harbor run` commands without running them |

mini-swe-agent uses the lane's prompt config in `run_config/mswea/<lane>.yaml`.

Rerunning with the same job name resumes that job, and harbor refuses to resume it with a
different config (`FileExistsError`). Set `JOB_NAME` when you rerun with anything changed: a
different `K`, task filter or extra flags. Only the part of `MODEL` after the last `/` goes into
the default name.

To browse results, run `harbor view results/<lane>`.

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
