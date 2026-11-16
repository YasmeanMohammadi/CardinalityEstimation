# Phase 1 Progress Report
### Neural Cardinality Estimation for Cypher Graph Queries — MLP vs. GNN
**Yasmin · November 16, 2026**

## 1. Objective and dataset

The project compares two neural architectures — an MLP on engineered query features and a GNN on
the query pattern — against Neo4j's native estimator for predicting the number of matches of a
Cypher pattern before execution. Phase 1 delivers a reproducible measurement pipeline and a pilot
dataset of measured queries.

I use the Neo4j **Recommendations** graph (demo server, Neo4j 2026.05.0): **28,866 nodes**,
**166,262 relationships**. Relationship types are `RATED` (100,004), `ACTED_IN` (35,910),
`IN_GENRE` (20,340), `DIRECTED` (10,007); these sum to 166,261, with the remaining 1 edge being an
internal Bloom relationship. The `RATED` edge carries one property, `rating`, with 10 half-step
values (0.5–5.0) skewed toward 4.0 — the skew that makes cardinality hard to estimate.

## 2. Workload generation and measurement

A single seeded Python collector (`build_pilot_dataset.py`) discovers the schema, generates the
workload, and measures each query. Query **templates** fix the pattern and predicate *shape*;
instances vary only the predicate value, so entire templates can be held out in Phase 2.

Three pattern families are defined by their **number of relationships** (verified against the
output CSV): **1-edge** `(User)-[RATED]->(Movie)`; **2-edge** co-rating
`(Movie)<-[RATED]-(User)-[RATED]->(Movie)` and the genre chain
`(User)-[RATED]->(Movie)-[IN_GENRE]->(Genre)`; **3-edge** chain
`(User)-[RATED]->(Movie)-[IN_GENRE]->(Genre)<-[IN_GENRE]-(Movie)` and star
`(Movie)<-[RATED]-(User), (Movie)<-[RATED]-(User), (Movie)-[IN_GENRE]->(Genre)`. Predicate values
(equality, `>=`/`<=` thresholds, ranges, and impossible values for zero-matches) are drawn from
the observed rating distribution.

For every instance the collector runs two queries built from one template: a `RETURN count(*)`
query for the **true cardinality** `c` (0 is valid), and `EXPLAIN` on the non-aggregate form for
Neo4j's **estimate** `ĉ`, read from the plan operator that emits the full pattern after filters
(the operator name is logged for auditing).

**Worked example** (`(User)-[RATED]->(Movie) WHERE rating = 0.5`): true count **1,101**; the
`EXPLAIN` `Filter` operator estimates **5,000** (a flat ~1/20 selectivity that ignores the skew),
a **Q-error of 4.54**.

## 3. Pilot results and observations

**69 instances, 21 templates, 3 families, 6 zero-result queries**; no errors or ambiguous plans.
True cardinalities span 0 to ~521M (median ~98,900). Error uses the proposal's zero-safe
`Q = max((c+1)/(ĉ+1), (ĉ+1)/(c+1))`.

| Family | n | median Q-error | p90 Q-error | max Q-error |
|---|---|---|---|---|
| 1-edge | 32 | **2.20** | 6.46 | 30,002 |
| 2-edge | 24 | **8.75** | 22.23 | 663,989 |
| 3-edge | 13 | **8.41** | 87,079 | 10,099,599 |
| Overall | 69 | 4.54 | 50.12 | 10,099,599 |

**Observations.** (1) Estimation error varies substantially across families: single-hop queries
are estimated most accurately (median 2.20), while the two- and three-edge families are much
harder (8.75 and 8.41) — the error is *not* monotonic in hop count. A plausible explanation is
Neo4j's attribute-/join-independence assumptions, but the pilot can only flag this as a candidate,
not confirm it as the cause. (2) Zero-result queries expose the largest errors: Neo4j assigns
large positive cardinalities (up to ~10M) to patterns that return nothing, producing the three
worst Q-errors. Whether a learned model predicts these zeros correctly is not automatic and will
be tested directly in Phase 2.

## 4. MLP and GNN input designs

Both encoders consume the **same** vocabulary (labels, relationship types, predicate operators,
normalized rating) and the **same** graph-statistics block (label/relationship frequencies, degree
summaries), so the only variable under test is the representation.

**MLP** — a fixed-length padded vector: per-node label one-hot and predicate; a **typed adjacency**
in which each edge record stores its *source and destination node indices* alongside its type and
predicate (so edge features are unambiguously bound to nodes, even in the star where three edges
share a center); a flattened adjacency block (removed in the planned ablation); and the global
statistics. Target `ŷ = log(1 + c)`.

**GNN** — the query as a small directed typed graph: nodes carry label and predicate features,
edges carry relationship type, predicate, and a direction flag, consumed by an edge-aware GINE
layer. Each relationship is materialized as both a forward and a reverse edge so messages
propagate from both endpoints while the Cypher direction is preserved by the flag. Global
statistics are concatenated at readout.

## 5. Phase 2 plan

Scale the generator to ~1,500–3,000 instances; implement the MLP, the GNN, and an XGBoost baseline
on the MLP features; split **by template** to prevent leakage; train with log-cardinality MSE and
early stopping. Evaluate median/p90 Q-error against Neo4j, broken down by family and selectivity,
run the adjacency ablation, and specifically measure how each model handles zero-result queries.

*Full engineering detail, plan dumps, and exact feature vectors are in the technical record
(`PHASE1_REPORT.md`); the pipeline and data are in `build_pilot_dataset.py` and
`pilot_dataset.csv`.*
