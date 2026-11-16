# Phase 1 Technical Record — Recommendations Pilot Dataset

**Project:** Neural Cardinality Estimation for Cypher Graph Queries (MLP vs. GNN)
**Scope:** detailed engineering record. The two-page submission is `PHASE1_SUMMARY.md`.
**Date:** 2026-11-16

---

## 1. Graph and workload

**Graph.** Neo4j Sandbox / demo **Recommendations** database.

| Property | Value |
|---|---|
| Neo4j version | 2026.05.0 (enterprise) |
| Database name | `recommendations` |
| Nodes | 28,866 |
| Relationships | 166,262 |

**Label frequencies** (lightweight statistics fed to both models):

| Label | Count | Label | Count |
|---|---|---|---|
| Person | 19,047 | Director | 4,091 |
| Actor | 15,443 | User | 671 |
| Movie | 9,125 | Genre | 20 |

> Labels overlap (every `Actor`/`Director` is also a `Person`), so these counts are **not**
> additive and do not sum to the 28,866 node total. Two internal Bloom nodes
> (`_Bloom_Perspective_`, `_Bloom_Scene_`) are ignored.

**Relationship-type frequencies.**

| Type | Count |
|---|---|
| RATED | 100,004 |
| ACTED_IN | 35,910 |
| IN_GENRE | 20,340 |
| DIRECTED | 10,007 |
| `_Bloom_HAS_SCENE_` (internal) | 1 |
| **Total** | **166,262** |

> Reconciliation: the four content relationship types sum to **166,261**; the remaining **1**
> is the internal `_Bloom_HAS_SCENE_` edge, giving the reported total of 166,262. The workload
> uses only the four content types.

**Rating property.** The `keys(r)` probe shows `RATED` carries one property, **`rating`**, with
10 distinct half-step values (0.5–5.0), skewed toward high ratings:

| rating | freq | rating | freq | rating | freq |
|---|---|---|---|---|---|
| 4.0 | 28,750 | 3.5 | 10,538 | 1.0 | 3,326 |
| 3.0 | 20,064 | 4.5 | 7,723 | 1.5 | 1,687 |
| 5.0 | 15,095 | 2.0 | 7,271 | 0.5 | 1,101 |
| | | 2.5 | 4,449 | | |

**Pattern families and predicate sampling.** Predicate values come directly from this observed
distribution (not hand-picked), so the workload spans common, selective, and impossible filters.
**Family names are literal: a family's name is the number of relationships in its `MATCH`**, and
this is verified against the generated CSV (1-edge rows contain exactly one relationship, etc.).

| Family | Base pattern (edges) | What varies |
|---|---|---|
| 1-edge | `(User)-[RATED]->(Movie)` **(1)** | equality on each rating value; `>=` / `<=` thresholds; `BETWEEN` ranges; impossible-value zero-matches |
| 2-edge | co-rating `(Movie)<-[RATED]-(User)-[RATED]->(Movie)` **(2)** and genre `(User)-[RATED]->(Movie)-[IN_GENRE]->(Genre)` **(2)** | filter first/second/both rating edges; rating threshold on the genre chain; zero-match |
| 3-edge | chain `(User)-[RATED]->(Movie)-[IN_GENRE]->(Genre)<-[IN_GENRE]-(Movie)` **(3)** and star `(Movie)<-[RATED]-(User), (Movie)<-[RATED]-(User), (Movie)-[IN_GENRE]->(Genre)` **(3)** | rating thresholds on one or both rater edges; zero-match |

Everything is seeded (`SEED = 42`) and the schema is discovered at runtime, so a re-run on the
same graph reproduces the workload exactly.

---

## 2. Measurement method

Each query instance is built as **two queries from one template** and both are run:

- **Ground truth** `c` — a `RETURN count(*)` query (0 is a valid measured count).
- **Neo4j estimate** `ĉ` — `EXPLAIN` on the matching *non-aggregate* query. The estimate is the
  `EstimatedRows` of the shallowest non-projection plan operator whose identifiers cover every
  returned pattern variable, i.e. the operator that emits the complete pattern **after filters**.
  The chosen operator name is saved in `estimate_operator` for auditing. The estimate is taken
  from the non-aggregate form on purpose: the `count(*)` plan's top operator is an aggregation
  that emits a single row, which is not the pattern cardinality.

**Worked query pair** (row `q0002`, one-edge):

```cypher
-- ground truth
MATCH (u:User)-[r:RATED]->(m:Movie) WHERE r.rating = 0.5
RETURN count(*) AS actual_cardinality          -- actual_cardinality = 1101

-- non-aggregate, EXPLAIN'd for the estimate
EXPLAIN MATCH (u:User)-[r:RATED]->(m:Movie) WHERE r.rating = 0.5
RETURN u, r, m
```

EXPLAIN plan (`ProduceResults → Filter → Expand(All) → NodeByLabelScan`):

```
| Operator        | Id | Details                               | Estimated Rows |
| +ProduceResults |  0 | u, r, m                               |          5000  |
| +Filter         |  1 | r.rating = $autodouble_0 AND m:Movie  |          5000  |  <- selected
| +Expand(All)    |  2 | (u)-[r:RATED]->(m)                    |        100004  |
| +NodeByLabelScan|  3 | u:User                                |           671  |
```

→ `estimate_operator = Filter`, `neo4j_estimate = 5000.2`, `actual = 1101`,
zero-safe **Q-error = (5000.2+1)/(1101+1) = 4.54**. Neo4j applies a flat ~1/20 equality
selectivity (5,000 ≈ 100,004 × 0.05) and ignores the skew that makes `0.5` the rarest rating.

---

## 3. Pilot results

**69 query instances across 21 templates and 3 families; 6 zero-result queries.** No errors or
ambiguous plan selections were logged.

| Family | Queries | Templates | Zero-result |
|---|---|---|---|
| 1-edge | 32 | 7 | 2 |
| 2-edge | 24 | 8 | 2 |
| 3-edge | 13 | 6 | 2 |
| **Total** | **69** | **21** | **6** |

**Cardinality distribution** (true counts): min 0 · median 98,903 · p90 ≈ 50.1M · max ≈ 521M.
The 3-edge patterns reach hundreds of millions of rows, giving the wide dynamic range the
`log(1+c)` target is meant to handle.

**Neo4j Q-error** (zero-safe `Q = max((c+1)/(ĉ+1), (ĉ+1)/(c+1))`):

| Family | n | median Q | p90 Q | max Q |
|---|---|---|---|---|
| 1-edge | 32 | 2.20 | 6.46 | 30,002 |
| 2-edge | 24 | 8.75 | 22.23 | 663,989 |
| 3-edge | 13 | 8.41 | 87,079 | 10,099,599 |
| **Overall** | **69** | **4.54** | **50.12** | **10,099,599** |

**Observations (stated as observations, not proven causes).**
- **Estimation error varies substantially across query families.** Single-hop patterns are
  estimated most accurately (median Q-error 2.20); the multi-hop families show markedly higher
  error (2-edge 8.75, 3-edge 8.41). The error is **not monotonic in hop count** — the two-edge
  co-rating patterns are already as hard as the three-edge patterns in the median.
- **A possible explanation** is Neo4j's attribute- and join-independence assumptions, which the
  co-rating join stresses. The pilot can *identify* this as a candidate explanation but cannot
  *confirm* it as the cause; isolating it is Phase-2 work (e.g. the adjacency ablation).
- **Zero-result queries expose the largest errors.** Neo4j assigns large positive cardinalities
  (30k–10.1M) to patterns that return no matches; the three worst Q-errors in the pilot are all
  zero-result queries. Note this is about Neo4j's estimator here — not a claim that it can never
  output zero. Whether a learned model predicts these zeros correctly is **not guaranteed** and
  is itself something Phase 2 must test.
- **Flat equality selectivity.** Every `rating = v` query receives the same ~5,000 estimate
  regardless of `v`, so error tracks how far each rating's true frequency sits from the mean.

**Dataset columns** (`pilot_dataset.csv`): `query_id, template_id, pattern_family, cypher,
parameters_json, actual_cardinality, neo4j_estimate, estimate_operator`. Grouping by
`template_id` lets Phase 2 hold out entire templates for a leakage-free split.

---

## 4. Model-input sketch

Both encodings describe the **same** example query `q0002`
(`(User)-[RATED {rating=0.5}]->(Movie)`) from the **same** vocabulary — labels
`[User, Movie, Genre, Person, Actor, Director]`, relationship types
`[RATED, IN_GENRE, ACTED_IN, DIRECTED]`, predicate operators `[=, >=, <=, >, <, between]`,
rating normalized to `[0,1]` as `(v−0.5)/4.5` — and the **same** graph-statistics block.

### 4a. MLP: fixed-length padded vector with a *typed* adjacency

Slots padded to `MAX_NODES = 6`, `MAX_EDGES = 5` (covers the 3-edge star's 4 nodes / 3 edges
with headroom). To remove the ambiguity the reviewer flagged, **each edge slot explicitly stores
its source and destination node indices**, so an edge's type/predicate is bound to specific node
slots rather than floating in a separate list.

| Block | Layout | Values for `q0002` |
|---|---|---|
| Node labels | `MAX_NODES × 6` one-hot | n0=User `[1,0,0,0,0,0]`, n1=Movie `[0,1,0,0,0,0]`, n2–n5 = zeros |
| Node predicates | `MAX_NODES × (flag + op + value)` | all zero (no node-level predicate here) |
| **Edge records (typed adjacency)** | `MAX_EDGES × (src_onehot[6] + dst_onehot[6] + type_onehot[4] + pred[1 flag + 6 op + 2 value])` | e0: src=n0 `[1,0,0,0,0,0]`, dst=n1 `[0,1,0,0,0,0]`, type=RATED `[1,0,0,0]`, flag=1, op `=`→`[1,0,0,0,0,0]`, lo=hi=`(0.5−0.5)/4.5=0.0`; e1–e4 = zeros |
| Plain adjacency (ablation handle) | flattened `6×6` directed | `A[0,1]=1`; this is the block the proposal's ablation removes |
| Graph stats | label freqs ∥ rel freqs ∥ degree summaries | User 0.023, Movie 0.316 … ; RATED 0.602, IN_GENRE 0.122 … ; avg User→RATED out-deg 149.0, avg Movie←RATED in-deg 10.96 |

The `(src_onehot, dst_onehot)` indices are what make the flattened encoding unambiguous for the
2- and 3-edge patterns (e.g. the star, where three edges all touch the same center node). The
network predicts `ŷ = log(1 + c)`.

### 4b. GNN: directed, typed query graph with reverse message passing

Same labels, types, predicates, and statistics, kept as a graph:

```
        RATED  {op:"=", value:0.0(norm), rel_freq:0.602, direction:forward}
 (u:User) ───────────────────────────────────────────────▶ (m:Movie)
  node features:                                   node features:
   label=User one-hot                               label=Movie one-hot
   label_freq=0.023                                 label_freq=0.316
   out/in-degree summary                            out/in-degree summary
```

- **Nodes** = query variables (label one-hot, any node predicate, label frequency, degree summary).
- **Edges** = typed directed relationships (relationship-type one-hot, edge predicate `(op, value)`,
  relationship frequency, direction flag) — the natural input for an edge-aware **GINE** layer.
- **Reverse message passing.** Each relationship is materialized as **both** a forward and a
  backward edge so information propagates from both endpoints, while the original Cypher direction
  is preserved via a direction flag on the edge features (equivalently, distinct forward/reverse
  edge types). Without this, a leaf node in the pattern could never receive messages from its
  parent.
- **Global features** appended at readout are the *same* graph-statistics block the MLP uses, so
  the only representational difference under test is explicit message passing vs. a flattened
  (typed) adjacency — isolating RQ2.

---

## 5. Reproduce

```bash
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env            # fill in Neo4j connection details
.venv/bin/python build_pilot_dataset.py --validate 5   # spot-check vs Neo4j Browser
.venv/bin/python build_pilot_dataset.py                 # full pilot
```

**Artifacts:** `build_pilot_dataset.py` · `pilot_dataset.csv` (69 rows) · `schema_report.json` ·
`pilot_summary.json` · `PHASE1_SUMMARY.md` (submission) · this record.

**Next (Phase 2):** scale to ~1,500–3,000 instances, implement the two encoders above plus the
XGBoost baseline, split by `template_id`, run the adjacency ablation, and explicitly measure how
each model handles zero-result queries.
