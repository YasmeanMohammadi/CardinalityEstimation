#!/usr/bin/env python3
"""
build_pilot_dataset.py
======================

Phase 1 collector for the "Neural Cardinality Estimation for Cypher Graph Queries"
project. It builds a reproducible pilot dataset of measured Cypher queries from a
Neo4j graph (designed against the Neo4j Sandbox "Recommendations" database, but it
discovers the schema so it works on any graph that has a RATED-style edge).

For every generated query instance the script:
  1. Builds a count query and a matching non-aggregate query from the same template.
  2. Runs the count query to get the true cardinality c (an integer; 0 is valid).
  3. Runs EXPLAIN on the non-aggregate query and extracts Neo4j's pre-execution
     estimate from the operator that emits the complete pattern *after* filters.
  4. Writes one CSV row. Errors and ambiguous plan selections are logged separately.

Outputs (written next to this script, or to --outdir):
  schema_report.json     graph metadata + RATED property distribution
  pilot_dataset.csv       query_id, template_id, pattern_family, cypher,
                          parameters_json, actual_cardinality, neo4j_estimate,
                          estimate_operator
  pilot_summary.json      per-family counts, cardinality distribution, Q-error
  errors.log              queries that raised an error
  ambiguous_plans.log     queries where the matched-pattern operator was ambiguous

Credentials (Neo4j Sandbox "Connect via drivers" tab) are read from, in order:
  --uri/--user/--password CLI flags, then NEO4J_URI / NEO4J_USERNAME /
  NEO4J_PASSWORD env vars, then a local `.env` file (KEY=VALUE lines).

Typical use:
  # 1) confirm a handful of rows against Neo4j Browser first
  python build_pilot_dataset.py --validate 5
  # 2) then generate the full pilot
  python build_pilot_dataset.py

Requires: neo4j>=5 (pip install -r requirements.txt).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Optional

from neo4j import GraphDatabase

# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #
SEED = 42
HERE = Path(__file__).resolve().parent

# Operators that do not themselves "emit the pattern" -- they re-shape or project
# rows that a lower operator already produced. We skip them when searching for the
# operator that emits the complete matched pattern after filters.
SKIP_OPERATORS = {
    "ProduceResults",
    "Projection",
    "EagerAggregation",
    "Eager",
    "Distinct",
    "Optional",
    "Limit",
    "Skip",
    "Sort",
    "Top",
}


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #
def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str, str, Optional[str]]:
    dotenv = _load_dotenv(HERE / ".env")

    def pick(flag, *env_keys):
        if flag:
            return flag
        for k in env_keys:
            if os.environ.get(k):
                return os.environ[k]
            if dotenv.get(k):
                return dotenv[k]
        return None

    uri = pick(args.uri, "NEO4J_URI", "NEO4J_BOLT_URL")
    user = pick(args.user, "NEO4J_USERNAME", "NEO4J_USER")
    password = pick(args.password, "NEO4J_PASSWORD")
    database = pick(args.database, "NEO4J_DATABASE")

    missing = [n for n, v in [("uri", uri), ("user", user), ("password", password)] if not v]
    if missing:
        sys.exit(
            "ERROR: missing connection details: "
            + ", ".join(missing)
            + "\nProvide them via --uri/--user/--password, environment variables "
            "(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD), or a .env file.\n"
            "In Neo4j Sandbox these are on the 'Connection details' / "
            "'Connect via drivers' tab."
        )
    return uri, user, password, database


# --------------------------------------------------------------------------- #
# Schema inspection
# --------------------------------------------------------------------------- #
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def run_scalar(session, cypher: str, **params):
    # Take the first record only; some procedures return multiple rows and
    # .single() would warn. We only ever select one scalar column.
    rec = next(iter(session.run(cypher, **params)), None)
    return None if rec is None else rec.value()


def inspect_schema(session) -> dict[str, Any]:
    info: dict[str, Any] = {}

    # Version / edition
    try:
        rec = next(iter(session.run(
            "CALL dbms.components() YIELD name, versions, edition "
            "RETURN name AS name, versions AS versions, edition AS edition"
        )), None)
        info["neo4j_name"] = rec["name"]
        info["neo4j_version"] = rec["versions"][0] if rec["versions"] else None
        info["neo4j_edition"] = rec["edition"]
    except Exception as exc:  # noqa: BLE001
        info["neo4j_version_error"] = str(exc)

    # Database name
    try:
        info["database"] = run_scalar(session, "CALL db.info() YIELD name RETURN name")
    except Exception:  # noqa: BLE001
        info["database"] = None

    info["node_count"] = run_scalar(session, "MATCH (n) RETURN count(n) AS c")
    info["relationship_count"] = run_scalar(session, "MATCH ()-[r]->() RETURN count(r) AS c")
    info["labels"] = run_scalar(session, "CALL db.labels() YIELD label RETURN collect(label) AS l") or []
    info["relationship_types"] = (
        run_scalar(
            session,
            "CALL db.relationshipTypes() YIELD relationshipType "
            "RETURN collect(relationshipType) AS t",
        )
        or []
    )

    # Label frequencies (lightweight graph statistics for the model inputs)
    label_freq = {}
    for lbl in info["labels"]:
        label_freq[lbl] = run_scalar(session, f"MATCH (n:`{lbl}`) RETURN count(n) AS c")
    info["label_frequencies"] = label_freq

    rel_freq = {}
    for rt in info["relationship_types"]:
        rel_freq[rt] = run_scalar(session, f"MATCH ()-[r:`{rt}`]->() RETURN count(r) AS c")
    info["relationship_frequencies"] = rel_freq

    # RATED relationship properties (proposal step 1)
    rated_prop = None
    rated_values: list[tuple[Any, int]] = []
    if "RATED" in info["relationship_types"]:
        key_rows = session.run(
            "MATCH (u:User)-[r:RATED]->(m:Movie) "
            "RETURN keys(r) AS relationship_properties, count(*) AS frequency "
            "ORDER BY frequency DESC"
        ).data()
        info["rated_property_keysets"] = key_rows
        # Flatten to a frequency over individual property names.
        prop_counts: dict[str, int] = {}
        for row in key_rows:
            for k in row["relationship_properties"]:
                prop_counts[k] = prop_counts.get(k, 0) + row["frequency"]
        info["rated_property_frequencies"] = prop_counts
        # Prefer a property literally called 'rating', else the most common key.
        if "rating" in prop_counts:
            rated_prop = "rating"
        elif prop_counts:
            rated_prop = max(prop_counts, key=prop_counts.get)

    if rated_prop and _IDENT_RE.match(rated_prop):
        value_rows = session.run(
            f"MATCH (:User)-[r:RATED]->(:Movie) WHERE r.`{rated_prop}` IS NOT NULL "
            f"RETURN r.`{rated_prop}` AS v, count(*) AS f ORDER BY f DESC LIMIT 100"
        ).data()
        rated_values = [(row["v"], row["f"]) for row in value_rows]

    info["rated_property"] = rated_prop
    info["rated_value_distribution"] = [
        {"value": v, "frequency": f} for v, f in rated_values
    ]
    return info


# --------------------------------------------------------------------------- #
# Query templates / workload
# --------------------------------------------------------------------------- #
@dataclass
class Instance:
    query_id: str
    template_id: str
    pattern_family: str
    match: str
    where: str  # "" or "WHERE ..."
    return_vars: list[str]
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def count_cypher(self) -> str:
        parts = [self.match]
        if self.where:
            parts.append(self.where)
        parts.append("RETURN count(*) AS actual_cardinality")
        return "\n".join(parts)

    @property
    def nonagg_cypher(self) -> str:
        parts = [self.match]
        if self.where:
            parts.append(self.where)
        parts.append("RETURN " + ", ".join(self.return_vars))
        return "\n".join(parts)


def _numeric_rating_values(schema: dict[str, Any]) -> list[float]:
    vals = []
    for entry in schema.get("rated_value_distribution", []):
        v = entry["value"]
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return sorted(set(vals))


def build_workload(schema: dict[str, Any]) -> list[Instance]:
    """Deterministically build 50-100 query instances across pattern families.

    Templates are gated on schema support. Instances that share query *structure*
    share a template_id, so entire templates can be held out in Phase 2.
    """
    rng = random.Random(SEED)
    prop = schema.get("rated_property")
    rel_types = set(schema.get("relationship_types", []))
    labels = set(schema.get("labels", []))
    values = _numeric_rating_values(schema)

    instances: list[Instance] = []
    counter = {"n": 0}

    def add(template_id, family, match, where, return_vars, params):
        counter["n"] += 1
        instances.append(
            Instance(
                query_id=f"q{counter['n']:04d}",
                template_id=template_id,
                pattern_family=family,
                match=match,
                where=where,
                return_vars=return_vars,
                params=params,
            )
        )

    if not (prop and "RATED" in rel_types and "User" in labels and "Movie" in labels):
        raise SystemExit(
            "This workload needs a (User)-[RATED {%s}]->(Movie) pattern. "
            "Discovered labels=%s rels=%s rated_property=%r. "
            "Adjust build_workload() for your schema." % (prop, labels, rel_types, prop)
        )

    p = prop  # short alias; validated identifier

    # Choose threshold / equality values from the observed distribution.
    if values:
        lo, hi = values[0], values[-1]
        eq_values = values  # every distinct rating gets an equality instance
        thresholds = [v for v in values if lo < v < hi] or values
        # a value that cannot occur -> guaranteed zero-match
        zero_eq = round(hi + 1.0, 2)
        between_pairs = []
        if len(values) >= 4:
            q = len(values) // 4
            between_pairs = [
                (values[q], values[2 * q]),
                (values[2 * q], values[-1]),
                (values[0], values[q]),
            ]
    else:  # non-numeric ratings: fall back to a couple of generic buckets
        eq_values, thresholds, zero_eq, between_pairs = [], [], None, []

    # ----- Family 1: one edge  (User)-[RATED]->(Movie) --------------------- #
    fam1 = "1-edge"
    m1 = "MATCH (u:User)-[r:RATED]->(m:Movie)"
    rv1 = ["u", "r", "m"]

    add("T1a_1edge_unfiltered", fam1, m1, "", rv1, {})

    for v in eq_values:
        add("T1b_1edge_rating_eq", fam1, m1, f"WHERE r.`{p}` = $v", rv1, {"v": v})

    for v in thresholds:
        add("T1c_1edge_rating_ge", fam1, m1, f"WHERE r.`{p}` >= $v", rv1, {"v": v})

    for v in thresholds:
        add("T1d_1edge_rating_le", fam1, m1, f"WHERE r.`{p}` <= $v", rv1, {"v": v})

    for lo_v, hi_v in between_pairs:
        add(
            "T1e_1edge_rating_between",
            fam1,
            m1,
            f"WHERE r.`{p}` >= $lo AND r.`{p}` <= $hi",
            rv1,
            {"lo": lo_v, "hi": hi_v},
        )

    if zero_eq is not None:
        add("T1f_1edge_zero_eq", fam1, m1, f"WHERE r.`{p}` = $v", rv1, {"v": zero_eq})
        add("T1g_1edge_zero_gt", fam1, m1, f"WHERE r.`{p}` > $v", rv1, {"v": zero_eq})

    # Edge-count naming is literal throughout: a family's name is its number of
    # relationships in the MATCH, so "3-edge" templates contain exactly 3 edges.
    def andw(extra: str) -> str:
        """Append a predicate to a MATCH that already carries a WHERE guard."""
        return "AND " + extra if extra else ""

    # ----- Family 2: genuine two-edge patterns ----------------------------- #
    fam2 = "2-edge"
    # (a) co-rating chain: two movies rated by the same user (2 edges)
    m2 = "MATCH (m1:Movie)<-[r1:RATED]-(u:User)-[r2:RATED]->(m2:Movie)\nWHERE m1 <> m2"
    rv2 = ["m1", "r1", "u", "r2", "m2"]

    add("T2a_2edge_corate_unfiltered", fam2, m2, "", rv2, {})
    for v in (thresholds[:5] or eq_values[:5]):
        add("T2b_2edge_corate_first_ge", fam2, m2, andw(f"r1.`{p}` >= $v"), rv2, {"v": v})
    for v in (thresholds[:5] or eq_values[:5]):
        add("T2c_2edge_corate_second_ge", fam2, m2, andw(f"r2.`{p}` >= $v"), rv2, {"v": v})
    for v in (thresholds[:5] or eq_values[:5]):
        w = rng.choice(thresholds or eq_values)
        add("T2d_2edge_corate_both_ge", fam2, m2,
            andw(f"r1.`{p}` >= $v AND r2.`{p}` >= $w"), rv2, {"v": v, "w": w})
    if zero_eq is not None:
        add("T2e_2edge_corate_zero", fam2, m2, andw(f"r1.`{p}` = $v"), rv2, {"v": zero_eq})

    # (b) rating -> genre chain (2 edges): RATED then IN_GENRE
    if "IN_GENRE" in rel_types and "Genre" in labels:
        m2g = "MATCH (u:User)-[r:RATED]->(m:Movie)-[:IN_GENRE]->(g:Genre)"
        rv2g = ["u", "r", "m", "g"]
        add("T2f_2edge_genre_unfiltered", fam2, m2g, "", rv2g, {})
        for v in (thresholds[:5] or eq_values[:5]):
            add("T2g_2edge_genre_rating_ge", fam2, m2g, f"WHERE r.`{p}` >= $v", rv2g, {"v": v})
        if zero_eq is not None:
            add("T2h_2edge_genre_zero", fam2, m2g, f"WHERE r.`{p}` = $v", rv2g, {"v": zero_eq})

    # ----- Family 3: genuine three-edge patterns (schema-gated) ------------ #
    fam3 = "3-edge"
    if "IN_GENRE" in rel_types and "Genre" in labels:
        # (a) three-edge chain: rated movie -> its genre <- another movie
        #     edges: RATED, IN_GENRE, IN_GENRE
        m3c = (
            "MATCH (u:User)-[r:RATED]->(m:Movie)-[:IN_GENRE]->(g:Genre)"
            "<-[:IN_GENRE]-(m2:Movie)\nWHERE m <> m2"
        )
        rv3c = ["u", "r", "m", "g", "m2"]
        add("T3a_3edge_chain_unfiltered", fam3, m3c, "", rv3c, {})
        for v in (thresholds[:5] or eq_values[:5]):
            add("T3b_3edge_chain_rating_ge", fam3, m3c, andw(f"r.`{p}` >= $v"), rv3c, {"v": v})
        if zero_eq is not None:
            add("T3c_3edge_chain_zero", fam3, m3c, andw(f"r.`{p}` = $v"), rv3c, {"v": zero_eq})

        # (b) three-edge star on a movie: two raters plus a genre
        #     edges: RATED, RATED, IN_GENRE
        m3s = (
            "MATCH (m:Movie)<-[r1:RATED]-(u1:User),\n"
            "      (m)<-[r2:RATED]-(u2:User),\n"
            "      (m)-[:IN_GENRE]->(g:Genre)\nWHERE u1 <> u2"
        )
        rv3s = ["m", "r1", "u1", "r2", "u2", "g"]
        add("T3d_3edge_star_unfiltered", fam3, m3s, "", rv3s, {})
        for v in (thresholds[:4] or eq_values[:4]):
            add("T3e_3edge_star_both_ge", fam3, m3s,
                andw(f"r1.`{p}` >= $v AND r2.`{p}` >= $v"), rv3s, {"v": v})
        if zero_eq is not None:
            add("T3f_3edge_star_zero", fam3, m3s, andw(f"r1.`{p}` = $v"), rv3s, {"v": zero_eq})

    return instances


# --------------------------------------------------------------------------- #
# Plan estimate extraction
# --------------------------------------------------------------------------- #
def _plan_type(node) -> str:
    # Neo4j may suffix the operator with the database, e.g. "Filter@recommendations".
    return node.get("operatorType", "").split("@", 1)[0]


def _plan_children(node) -> list:
    return node.get("children", []) or []


def _plan_identifiers(node) -> set[str]:
    return set(node.get("identifiers", []) or [])


def _plan_args(node) -> dict:
    # The driver exposes plan operator arguments under "args" (older builds
    # used "arguments"); EstimatedRows lives there.
    return node.get("args") or node.get("arguments") or {}


def _plan_estimated_rows(node):
    args = _plan_args(node)
    for key in ("EstimatedRows", "estimatedRows"):
        if key in args:
            return args[key]
    return None


def extract_pattern_estimate(plan: dict, pattern_vars: set[str]) -> tuple[Optional[float], str, bool]:
    """Return (estimated_rows, operator_type, ambiguous).

    The target is the operator closest to the root (but not a projection/result
    operator) whose identifiers include every pattern variable -- i.e. the
    operator that emits the complete matched pattern after all filters.
    """
    if plan is None:
        return None, "<no-plan>", True

    candidates: list[tuple[int, dict]] = []
    stack: list[tuple[dict, int]] = [(plan, 0)]
    while stack:
        node, depth = stack.pop()
        if _plan_type(node) not in SKIP_OPERATORS and pattern_vars.issubset(
            _plan_identifiers(node)
        ):
            candidates.append((depth, node))
        for child in _plan_children(node):
            stack.append((child, depth + 1))

    if not candidates:
        # Fall back to the root's estimate but flag it for audit.
        return _plan_estimated_rows(plan), _plan_type(plan) + "(fallback-root)", True

    candidates.sort(key=lambda dc: dc[0])
    shallowest_depth = candidates[0][0]
    tied = [c for c in candidates if c[0] == shallowest_depth]
    node = tied[0][1]
    ambiguous = len(tied) > 1
    return _plan_estimated_rows(node), _plan_type(node), ambiguous


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
def measure_instance(session, inst: Instance) -> dict[str, Any]:
    actual = run_scalar(session, inst.count_cypher, **inst.params)
    summary = session.run("EXPLAIN " + inst.nonagg_cypher, **inst.params).consume()
    plan = summary.plan
    estimate, operator, ambiguous = extract_pattern_estimate(plan, set(inst.return_vars))
    return {
        "query_id": inst.query_id,
        "template_id": inst.template_id,
        "pattern_family": inst.pattern_family,
        "cypher": inst.nonagg_cypher.replace("\n", " "),
        "parameters_json": json.dumps(inst.params, sort_keys=True),
        "actual_cardinality": int(actual),
        "neo4j_estimate": float(estimate) if estimate is not None else None,
        "estimate_operator": operator,
        "_ambiguous": ambiguous,
        "_count_cypher": inst.count_cypher.replace("\n", " "),
    }


def q_error(actual: int, estimate: Optional[float]) -> Optional[float]:
    """Zero-safe Q-error from the proposal: max((c+1)/(e+1), (e+1)/(c+1))."""
    if estimate is None:
        return None
    c = actual + 1.0
    e = estimate + 1.0
    return max(c / e, e / c)


def percentile(sorted_vals: list[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
CSV_COLUMNS = [
    "query_id",
    "template_id",
    "pattern_family",
    "cypher",
    "parameters_json",
    "actual_cardinality",
    "neo4j_estimate",
    "estimate_operator",
]


def write_csv(rows: list[dict], path: Path) -> None:
    import csv

    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def summarize(rows: list[dict]) -> dict[str, Any]:
    cards = sorted(r["actual_cardinality"] for r in rows)
    qerrs = sorted(
        q for r in rows if (q := q_error(r["actual_cardinality"], r["neo4j_estimate"])) is not None
    )
    by_family: dict[str, dict[str, Any]] = {}
    for r in rows:
        fam = r["pattern_family"]
        f = by_family.setdefault(fam, {"queries": 0, "templates": set(), "zero_results": 0})
        f["queries"] += 1
        f["templates"].add(r["template_id"])
        if r["actual_cardinality"] == 0:
            f["zero_results"] += 1
    for fam in by_family.values():
        fam["templates"] = len(fam["templates"])

    return {
        "total_queries": len(rows),
        "total_templates": len({r["template_id"] for r in rows}),
        "zero_result_queries": sum(1 for c in cards if c == 0),
        "by_family": by_family,
        "cardinality_distribution": {
            "min": cards[0] if cards else None,
            "p50_median": median(cards) if cards else None,
            "p90": percentile(cards, 0.90),
            "max": cards[-1] if cards else None,
        },
        "neo4j_q_error": {
            "count": len(qerrs),
            "median": median(qerrs) if qerrs else None,
            "p90": percentile(qerrs, 0.90),
            "max": qerrs[-1] if qerrs else None,
        },
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", help="Neo4j Bolt URI, e.g. neo4j+s://xxxx.databases.neo4j.io")
    ap.add_argument("--user", help="Neo4j username (sandbox default: neo4j)")
    ap.add_argument("--password", help="Neo4j password")
    ap.add_argument("--database", help="Database name (optional)")
    ap.add_argument("--outdir", default=str(HERE), help="Where to write outputs")
    ap.add_argument(
        "--validate",
        type=int,
        default=0,
        metavar="N",
        help="Validation mode: measure only the first N instances and print them "
        "(count query, estimate, operator) for manual comparison in Neo4j Browser.",
    )
    args = ap.parse_args()

    uri, user, password, database = resolve_credentials(args)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"ERROR: could not connect to {uri}: {exc}")

    sess_kwargs = {"database": database} if database else {}
    with driver.session(**sess_kwargs) as session:
        print("Inspecting schema ...")
        schema = inspect_schema(session)
        (outdir / "schema_report.json").write_text(json.dumps(schema, indent=2, default=str))
        print(
            f"  Neo4j {schema.get('neo4j_version')} ({schema.get('neo4j_edition')}), "
            f"db={schema.get('database')}, "
            f"{schema.get('node_count')} nodes / {schema.get('relationship_count')} rels"
        )
        print(f"  RATED property: {schema.get('rated_property')!r}; "
              f"{len(schema.get('rated_value_distribution', []))} distinct values")

        workload = build_workload(schema)
        if args.validate:
            workload = workload[: args.validate]
        print(f"Measuring {len(workload)} query instance(s) ...")

        rows: list[dict] = []
        errors: list[str] = []
        ambiguous: list[str] = []
        for i, inst in enumerate(workload, 1):
            try:
                row = measure_instance(session, inst)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{inst.query_id}\t{inst.template_id}\t{type(exc).__name__}: {exc}"
                              f"\t{inst.count_cypher.strip()}  ||  PARAMS={inst.params}")
                print(f"  [{i}/{len(workload)}] {inst.query_id} ERROR: {exc}")
                continue
            if row.pop("_ambiguous", False):
                ambiguous.append(
                    f"{row['query_id']}\t{row['template_id']}\toperator={row['estimate_operator']}"
                    f"\t{row['cypher']}"
                )
            rows.append(row)
            if args.validate:
                qe = q_error(row["actual_cardinality"], row["neo4j_estimate"])
                qe_str = f"{qe:.3f}" if qe is not None else "n/a"
                print(
                    f"\n  [{i}] {inst.query_id}  ({inst.template_id}, {inst.pattern_family})\n"
                    f"      count : {row.pop('_count_cypher')}  PARAMS={inst.params}\n"
                    f"        -> actual_cardinality = {row['actual_cardinality']}\n"
                    f"      estimate from EXPLAIN operator '{row['estimate_operator']}'"
                    f"  -> neo4j_estimate = {row['neo4j_estimate']}\n"
                    f"        Q-error = {qe_str}"
                )
            else:
                row.pop("_count_cypher", None)
                print(f"  [{i}/{len(workload)}] {inst.query_id} "
                      f"c={row['actual_cardinality']} est={row['neo4j_estimate']} "
                      f"op={row['estimate_operator']}")

    if errors:
        (outdir / "errors.log").write_text("\n".join(errors))
    if ambiguous:
        (outdir / "ambiguous_plans.log").write_text("\n".join(ambiguous))

    if args.validate:
        print(f"\nValidation done: {len(rows)} ok, {len(errors)} errors, "
              f"{len(ambiguous)} ambiguous. "
              "Compare the printed counts/estimates against Neo4j Browser, "
              "then run without --validate for the full pilot.")
        return

    write_csv(rows, outdir / "pilot_dataset.csv")
    summary = summarize(rows)
    (outdir / "pilot_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print("\n=== Pilot summary ===")
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nWrote {len(rows)} rows to {outdir / 'pilot_dataset.csv'}")
    if errors:
        print(f"  {len(errors)} errors logged to {outdir / 'errors.log'}")
    if ambiguous:
        print(f"  {len(ambiguous)} ambiguous plan selections logged to "
              f"{outdir / 'ambiguous_plans.log'}")


if __name__ == "__main__":
    main()
