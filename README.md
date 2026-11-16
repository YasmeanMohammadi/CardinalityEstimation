# Phase 1 — Recommendations pilot dataset

Reproducible collector for measured Cypher queries (true cardinality + Neo4j's
pre-execution estimate), feeding the MLP-vs-GNN cardinality-estimation study.

## Setup

```bash
# from the project root, using the project venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env     # then edit .env with your Neo4j Sandbox details
```

Connection details come from `.env`, from `NEO4J_URI` / `NEO4J_USERNAME` /
`NEO4J_PASSWORD` environment variables, or from `--uri/--user/--password` flags.

## Run

```bash
# 1) validate the first 5 instances (compare against Neo4j Browser by hand)
.venv/bin/python build_pilot_dataset.py --validate 5

# 2) generate the full pilot (50-100 instances)
.venv/bin/python build_pilot_dataset.py
```

## Outputs

| file | contents |
|------|----------|
| `schema_report.json` | version, db name, node/rel counts, label & RATED-value frequencies |
| `pilot_dataset.csv` | one row per query: `query_id, template_id, pattern_family, cypher, parameters_json, actual_cardinality, neo4j_estimate, estimate_operator` |
| `pilot_summary.json` | per-family counts, zero-result counts, cardinality distribution, Neo4j median/p90 Q-error |
| `errors.log` | queries that raised an error (if any) |
| `ambiguous_plans.log` | queries where the matched-pattern operator was ambiguous (if any) |

## Method notes

- **Ground truth** comes from a `RETURN count(*)` query; `0` is a valid measured count.
- **Neo4j estimate** is the `EstimatedRows` of the shallowest non-projection plan
  operator (from `EXPLAIN`) whose identifiers cover every returned pattern
  variable — i.e. the operator that emits the complete pattern *after* filters.
  The chosen operator name is saved in `estimate_operator` for auditing.
- **Q-error** uses the proposal's zero-safe definition
  `Q = max((c+1)/(ĉ+1), (ĉ+1)/(c+1))`.
- **Templates** group queries by structure (`template_id`) so entire templates can
  be held out for the Phase 2 train/val/test split.
- Everything is seeded (`SEED = 42`) and the schema is discovered at runtime, so a
  re-run on the same graph reproduces the same workload.
