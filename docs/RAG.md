# RAG: expense policy handbook search

**Production retrieval: vector search with `text-embedding-3-large` (the pre-declared winner) plus an exact section-ID lookup (`vector_id`, D31).**
**`AZURE_OPENAI_EMBED_DEPLOYMENT` = `text-embedding-3-large`.**

> **Caveat:** the 30 original evaluation questions were written by the same person (and assistant) who wrote the handbook, *with knowledge of its wording and section IDs*. They are therefore easier than real employee questions, and the absolute scores are optimistic. The comparison *between* methods is still informative, because every method faced the same questions. The 10 held-out questions were written before the ID shortcut existed, but by the same authors.

## What it does
- **Handbook:** the expense policy handbook is **generated from the database**, so every £ figure comes from `policy_limits`, `expense_categories` or `policy_city_hotel_caps` (D24).
  - Two versions are stored in `s3://akinsds1126-expense-artifacts/policy/<version>/`: 2025.1 (previous) and 2026.1 (latest). Only the latest is searched.
- **Chunks:** the handbook is split into **one chunk per subsection** (45 chunks), each starting with its heading path, for example "EXP-6 Travel and hotels > EXP-6.3 Hotels by city" (D25).
- **Embeddings:** each chunk is embedded with Azure OpenAI at 1,536 dimensions and stored in pgvector on RDS (`policy_chunks`, HNSW index), next to a Postgres full-text index (D27).
- **`search_policy(question)`:** returns the top passages, each with its **section ID**, so every answer can cite its source, for example "EXP-6.3".

| | |
|---|---|
| Ingest and evaluation job | One-off ECS Fargate task `akinsds1126-expense-ingest` (image `358a794`). Logs in CloudWatch `/ecs/akinsds1126-expense-ingest` |
| Run A (pre-declared comparison) | `eval/rag/20261125T151033Z-0393eda/` |
| Validation of the ID shortcut | `eval/rag/20261125T152933Z-358a794/` |
| Run B (production) | Task definition revision 2 with the command override `["--models","text-embedding-3-large","--prune-other-models"]`, so only -large chunk rows remain. That is now the committed default command in `infra/ecs/ingest-task-definition.json` |
| Reproduce the comparison | Run the task with the override `["--models","text-embedding-3-small,text-embedding-3-large","--evaluate"]`, then re-run the default command to prune back to -large |

## How it was measured, in plain words
- **Recall@3 / Recall@5:** for each question, is a correct section somewhere in the top 3 (or 5) passages? The table shows the share of questions where it is.
- **MRR (mean reciprocal rank):** how high up the first correct section appears. First place scores 1, second 0.5, third 0.33, and a miss scores 0, averaged over the questions. Higher means the right answer is nearer the top.
- **A hit:** the passage is the expected section, or one of its subsections. A question about "EXP-7" accepts "EXP-7.1".
- **Questions:** 30 labelled questions:
  - 16 paraphrases ("How long do I have to put in a claim?")
  - 5 exact terms or IDs ("What does EXP-6.3 say?", "hospitality register")
  - 4 multi-section questions ("Can I split a £300 client dinner…?")
  - 5 out-of-scope questions (weather, "which ETF should I buy?", the canteen menu, laptop passwords, annual leave)
  - The 25 in-scope questions are scored. The out-of-scope ones are used to check whether "no good answer" can be detected.
- **Model fairness:** each question is embedded with **the same model** as the chunks it's compared against.
- **Selection rule, written down before the run (D26):**
  1. highest Recall@5 (a difference of one question counts as a tie)
  2. then MRR (ties within 0.01)
  3. then the cheaper option (keyword < small < large)
  4. then the simpler one (keyword < vector < hybrid)

## The methods
- **Vector:** turns the question into an embedding and finds the chunks whose meaning is closest (cosine similarity). Good at paraphrases.
- **Keyword:** Postgres full-text search on the question's words, ranked by how well they match. A section ID in the question (like "EXP-4.2") is looked up directly. Good at exact terms.
- **Hybrid:** combines the vector and keyword rankings with Reciprocal Rank Fusion: each passage scores 1/(60 + its rank) in each list, and the scores are added. No tuning needed.
- **`vector_id`** (added after Run A, D31): if the question names a section ID that exists in the latest handbook, that section goes first, then vector results fill the rest. Without an ID it is exactly vector search.

## Run A: the pre-declared comparison (unchanged)
25 in-scope questions of 30.

| Method / model | Recall@3 | Recall@5 | MRR | Misses in top 5 | Recall@5: paraphrase / exact / multi |
|---|---|---|---|---|---|
| hybrid / text-embedding-3-large | 0.960 | 0.960 | 0.827 | q01 | 0.94 / 1.00 / 1.00 |
| hybrid / text-embedding-3-small | 0.880 | 0.960 | 0.838 | q01 | 0.94 / 1.00 / 1.00 |
| **vector / text-embedding-3-large** | 0.920 | 0.920 | **0.900** | q17, q18 | 1.00 / 0.60 / 1.00 |
| vector / text-embedding-3-small | 0.920 | 0.920 | 0.833 | q17, q18 | 1.00 / 0.60 / 1.00 |
| keyword | 0.720 | 0.800 | 0.627 | q01, q03, q06, q10, q16 | 0.69 / 1.00 / 1.00 |

**Applying the rule:**
1. The best Recall@5 is 0.96. One question is worth 0.04, so vector (0.92) ties with hybrid (0.96).
2. Among the tied options, vector/large has the clearly best MRR (0.900, against at most 0.838).
3. **Winner: vector / text-embedding-3-large.**

**What that hides:** vector missed both questions that cite a section ID (q17 "What does EXP-6.3 say?" and q18 "Show me section EXP-4.2"). Embeddings don't match ID strings reliably. Hybrid found them, but put the deadline question (q01) outside the top 5.

**Latest-only check:** every passage returned by every method was from version 2026.1 (`latest_only_ok`). The old "60 days" deadline text was never served. That second check only tests something when a method actually retrieved the deadline section; hybrid didn't for q01, so `latest_only_ok` is the real guarantee.

### Out-of-scope detection (top vector similarity)
| Model | In-scope min | In-scope mean | Out-of-scope max | Out-of-scope mean | Separable |
|---|---|---|---|---|---|
| text-embedding-3-small | 0.429 | 0.540 | 0.344 | 0.266 | yes |
| text-embedding-3-large | 0.392 | 0.563 | 0.301 | 0.228 | yes |

With -large, every out-of-scope question's best match scored at most 0.30, and every in-scope question's at least 0.39. **A threshold of about 0.35** is a reasonable starting point for "the handbook doesn't cover this" in Phase 5. It's only an indication, given 5 out-of-scope questions.

## The exact-ID shortcut: added after Run A and validated (D31)
The shortcut was added **after** seeing Run A, so it is a post-hoc change. To test it honestly, **10 new held-out questions** were written and committed (`dac8f3e`) **before** the shortcut was built or run:
- 5 cite IDs in varied phrasings: "explain section EXP-9.1", "what's in exp-4.2", "Can you summarise EXP-10 for me?", "…read Exp-7.2…", "Does EXP-6.1…"
- 5 are plain paraphrases, to show nothing else changes

| Question set | vector / large (before) | vector_id / large (after) |
|---|---|---|
| Original 30: Recall@3 / Recall@5 / MRR | 0.920 / 0.920 / 0.900 | **1.000 / 1.000 / 0.980** |
| Original 30: misses in top 5 | q17, q18 | none |
| Held-out 10: Recall@3 / Recall@5 / MRR | 0.500 / 0.600 / 0.470 | **0.900 / 0.900 / 0.900** |
| Held-out 10: misses in top 5 | h02, h03, h04, h10 | h10 |
| Held-out ID questions found (of 5) | 2 | **5** |
| Held-out paraphrases found (of 5) | 4 | 4 (unchanged) |

**In plain words:** the shortcut fixes questions that name a section, and changes nothing else. Without an ID, it *is* vector search, and the paraphrase results are identical.
- One held-out paraphrase is missed by **every** method: h10, "Can I claim a birthday present I bought for a colleague?", which should match EXP-10.1 ("gifts for colleagues"). That's a genuine retrieval gap: the question's wording ("birthday present") is far from the handbook's ("gifts for colleagues"). It's a candidate for a handbook wording or synonym improvement later. I haven't tuned anything for it.
- On the held-out set, hybrid and keyword also score 0.900 Recall@5 (MRR 0.850). Without the shortcut, vector alone would be clearly worst there (0.600).

## Production settings
- **Model:** `AZURE_OPENAI_EMBED_DEPLOYMENT = text-embedding-3-large`, called with `dimensions=1536`.
- **Method:** `search_policy` defaults to **`vector_id`**. It can be overridden with `EXPENSE_RAG_METHOD`, but only for experiments.
- **Data:** after Run B, `policy_chunks` holds only -large rows: 45 per version, 2025.1 not latest and 2026.1 latest.
- **Access:** reads run as `app_readonly`, and only `app_ingest` can write the RAG tables (D28).

## Limitations
- **Evaluation:** the original questions were written with knowledge of the handbook (see the caveat above), and the sets are small (25 + 10 scored questions). One question is worth 4–10 percentage points, so small differences aren't meaningful.
- **Generated handbook:** it's well structured and consistently worded, which flatters every method. Real policy documents are messier.
- **Out-of-scope threshold:** it rests on 5 questions; Phase 5 should confirm it.
- **Several cited IDs in one question:** the exact-ID lookup can let a section-level ID (for example `EXP-10`, with all its subsections) crowd out a second cited ID. That's a known follow-up (see the D27/D28 amendments in DECISIONS.md).
- **Wording gaps:** a question whose wording is far from the handbook's (h10) can still be missed. Synonym coverage or query rewriting by the agent (Phase 5) could help, measured on fresh questions.
