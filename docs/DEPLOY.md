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

The run link and the observed version are added here after the merge.

| Item | Value |
|---|---|
| Merge commit on main | _to be filled after merge_ |
| CI run | _to be filled_ |
| Deploy run | _to be filled_ |
| URL / version shown | _to be filled_ |
