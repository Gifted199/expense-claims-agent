# Deployment (Phase 6)

Decisions: D48–D52 in [DECISIONS.md](DECISIONS.md).

## What runs
**ECS Fargate service** `akinsds1126-expense-app-svc` on `akinsds1126-expense-cluster`. It runs one task (`akinsds1126-expense-app`, 1 vCPU / 3 GB) with three containers that start in order of health:

| Container | Image | Listens on | Health |
|---|---|---|---|
| `mcp` | `akinsds1126-expense-mcp:<sha>` | 127.0.0.1:8080 | `/health` |
| `agent-api` | `akinsds1126-expense-agent:<sha>` | 127.0.0.1:8000 | `/health` (starts after mcp is healthy) |
| `ui` | `akinsds1126-expense-ui:<sha>` | 0.0.0.0:8501, **the only exposed port** | `/_stcore/health` (starts after agent-api is healthy) |

- **Network:** security group `akinsds1126-expense-app-sg`. Inbound 8501 from the owner's IP only. Outbound 443 anywhere, and 5432 to the RDS SG.
- **Logs:** `/ecs/akinsds1126-expense-app`, with streams `mcp/…`, `agent/…` and `ui/…`.
- **Version:** the UI sidebar shows "Version `<short sha>`" for the running commit.

## Bootstrap, once by hand (2026-11-26)
1. **Images:** built on the laptop and pushed with Docker's ECR credential helper (D20), all tagged `cb79965ec5ca245eec328fc0821a799f29379bec`.
2. **Console steps** (the JSON is in `infra/`):
   1. log group `/ecs/akinsds1126-expense-app` (14 days)
   2. task role `akinsds1126-expense-app-task-role` (`infra/iam/app-task-role-*.json`)
   3. security group `akinsds1126-expense-app-sg`
   4. RDS SG inbound 5432 from that security group
   5. task definition from `infra/ecs/app-task-definition.json` (with the SHA filled in)
   6. the service (Fargate, desired 1, public IP on, no load balancer, circuit breaker with rollback)
   7. the deploy role `akinsds1126-expense-github-deploy` (`infra/iam/github-deploy-role-*.json`)
3. **Verified read-only:**
   - the service is ACTIVE, with deployment `akinsds1126-expense-app:1` COMPLETED
   - running 1/1, with the task and all three containers **HEALTHY**
   - `http://3.8.132.189:8501/_stcore/health` returns `ok` from the owner's laptop
   - the CloudWatch streams for all three containers exist

## Continuous deployment
The `deploy` job in `.github/workflows/ci.yml` (it was a separate `deploy.yml` until the first deploy failed; see the D51 amendment):
1. Runs only for a **push to `main`**, and only after the test job (`needs: lint-and-test`) has passed. A push to main is never cancelled midway.
2. Assumes `akinsds1126-expense-github-deploy` through GitHub OIDC. The trust is pinned to `repo:Gifted199/expense-claims-agent:ref:refs/heads/main`, and no AWS keys are stored in GitHub.
3. Builds and pushes the three images tagged with the commit SHA, skipping any tag that already exists.
4. Renders the task definition, deploys it, and waits for the service to be stable. The circuit breaker rolls back a failed deploy.
5. Prints the new URL. The public IP changes on each deploy, because there's no load balancer.

## Proof: the Phase 6 merge deploys itself
By your choice, there's no separate demo PR. The proof is the merge of the Phase 6 PR:
1. CI runs on `main`.
2. The `deploy` job starts by itself, with no manual step.
3. The service becomes stable.
4. The UI at the printed URL shows "Version `<main sha>`".

**What actually happened (2026-11-26):**
1. **PR #7 merged** (`77383d8`). The separate `deploy.yml` failed at the OIDC step.
2. **PR #8 merged** (`5b30bb8`). The deploy, now a job in CI, failed at the same step. The real cause was GitHub's ID-based `sub` claim (D51 amendment 2).
3. **You updated the trust policy** to `repo:Gifted199@238518570/expense-claims-agent@1470049029:ref:refs/heads/main` and re-ran the failed job. It succeeded with **no other manual step**: images built and pushed, the task definition registered, the service updated and stable.

| Item | Value |
|---|---|
| Merge commit on main | `5b30bb8` (PR #8: deploy job in CI, the UI `httpx` fix, the image import check, the README) |
| CI run | [45812983493](https://github.com/Gifted199/expense-claims-agent/actions/runs/45812983493). Attempt 1: tests ✅, deploy ❌ (OIDC `sub` mismatch). **Attempt 2: tests ✅, deploy ✅** (11:57–12:05 UTC) |
| Deploy result | Service `akinsds1126-expense-app-svc` on task definition **`akinsds1126-expense-app:2`**, deployment COMPLETED, 1/1 running. The task and all three containers are **HEALTHY** on images `akinsds1126-expense-{mcp,agent,ui}:5b30bb8c6d1d99a48d563a50e0907091826f07e2` |
| URL / version shown | **http://18.171.227.210:8501**: `/_stcore/health` returns `ok` from your IP. The sidebar shows **"Version 5b30bb8"**, confirmed in the browser |
| Live chat turn | CloudWatch `agent/agent-api/03bc2b95…` at 12:16:25 UTC: `agent_turn`, employee 1, tool `search_policy`, no guardrail flags. In the browser, the London hotel caps question was answered with citation **EXP-6.3**. The bootstrap task's `ModuleNotFoundError: httpx` appears only in the old task's UI stream (`813f41…`), and not in the new one |

After this, the temporary step that printed the OIDC claims was removed from the deploy job. The next merge to main deploys again with no manual step.
