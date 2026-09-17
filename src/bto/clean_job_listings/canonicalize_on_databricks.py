"""Full-history canonicalization at production scale: Spark for the
set-heavy work, the shared Python rules for every decision.

Called by:
    clean_job_listings.run (submits this module's entry point as a
    Databricks python_wheel_task and waits for it; also imports the SQL
    builders below so its own Statement API reads execute the same text)

Calls:
    build_matching_features (document, fingerprint, shingles, title and
    advertiser helpers), build_candidate_pairs (the locked 42 × 3 banding
    parameters), build_canonical_mapping (judge_pair, components_from_edges,
    rows_from_components, validate_candidate, graph_diagnostics)

The locked Spark / driver boundary:

SPARK — read the standardized snapshot N; one row per usable wording with
the recomputed matching document (asserted equal to the stored fingerprint),
its sorted distinct shingle-id array, its description-only normalized body,
stage and normalized title; the stored 128-value MinHash signature exploded
into the 42 × 3 bands and self-joined; the advertiser + title route from
memberships; the observed same-listing transitions with advertiser
continuity; the same-listing pair context; the candidate union with route
evidence; exact Jaccard on the arrays; the pair verdict through a thin UDF
around `build_canonical_mapping.judge_pair`; accepted edges only.

DRIVER — collect the accepted edges, the wordings' titles, the memberships
and the never-usable listings; `components_from_edges` (the guarded
union-find), `rows_from_components` (winner, ids, sentinels),
`validate_candidate`; write the run-scoped mapping scratch with
`canonicalized_at` NULL. Snapshot capture, stamping, joint validation,
publication and freshness stay in run.py — this module never publishes.

No Spark ML MinHashLSH, no GraphFrames, no second matching recipe: every
rule executed here is imported from the modules above.

Main functions:
    memberships_sql / transitions_sql / documents_sql / never_usable_sql
        -> the source query text shared with run.py
    prepare_wording / band_rows / judge_row -> the UDF bodies (pure)
    canonicalize(spark, frames)  -> (rows, diagnostics)
    main(argv)                   -> the job entry point
"""

import argparse
import configparser
import hashlib
import importlib.metadata
import json
import logging
import re
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

from ..storage.databricks import freshness
from ..storage.databricks.freshness import pinned
from . import build_candidate_pairs as candidates_module
from . import build_canonical_mapping as canonical
from . import build_matching_features as features
from . import extend_canonical_mapping as extend

log = logging.getLogger(__name__)

BANDS, ROWS_PER_BAND = candidates_module.CANDIDATE_LSH_PARAMS   # 42 × 3
SIGNATURE_LENGTH = features.NUM_PERM                            # 128
BANDED_POSITIONS = BANDS * ROWS_PER_BAND                        # 0–125 used
_TWO_63 = 1 << 63
_TWO_64 = 1 << 64
ROUTE_NAMES = ("lsh", "advertiser_title", "listing_history")
DIAGNOSTICS_MARKER = "BTO_DIAGNOSTICS "


# The code that executes inside the Databricks job, as wheel-relative paths:
# the entry point and every `bto` module it imports, transitively (freshness
# brings connection with it). A deployed wheel must hold exactly these bytes
# before it may canonicalize — code identity, enforced at submission time by
# run.py, never a data-version column. Kept in step with the real
# import graph by a test.
SPARK_JOB_MODULES = (
    "bto/clean_job_listings/canonicalize_on_databricks.py",
    "bto/clean_job_listings/build_canonical_mapping.py",
    "bto/clean_job_listings/build_candidate_pairs.py",
    "bto/clean_job_listings/build_matching_features.py",
    "bto/clean_job_listings/extend_canonical_mapping.py",
    "bto/storage/databricks/freshness.py",
    "bto/storage/databricks/connection.py",
)
# Package initializers execute on import before any module above: they are
# part of what runs, so they are hashed too (a wheel could carry code there
# while every module matched).
PACKAGE_INITS = (
    "bto/__init__.py",
    "bto/clean_job_listings/__init__.py",
    "bto/storage/__init__.py",
    "bto/storage/databricks/__init__.py",
)
IDENTITY_FILES = SPARK_JOB_MODULES + PACKAGE_INITS
# The job runs the wheel's console entry point of this name (python_wheel_task,
# package "bto"); the archive must map it to exactly this function, or the
# matched modules would never execute.
ENTRY_POINT_NAME = "bto-canonicalize"
ENTRY_POINT_TARGET = "bto.clean_job_listings.canonicalize_on_databricks:main"
DISTRIBUTION = "bto"
_DIST_INFO = re.compile(r"^bto-[^/]+\.dist-info/")


def _source_root():
    """The parent of the installed `bto` package: the checkout's `src/`
    under an editable install, site-packages otherwise."""
    import bto
    return Path(bto.__file__).resolve().parent.parent


def module_digests(read):
    """{wheel-relative path: sha256 hex, or None when missing} over
    IDENTITY_FILES in that fixed order; `read(path)` returns the bytes or
    None."""
    digests = {}
    for path in IDENTITY_FILES:
        data = read(path)
        digests[path] = None if data is None else hashlib.sha256(data).hexdigest()
    return digests


def source_digests(root=None):
    root = Path(root) if root else _source_root()

    def read(path):
        file = root / path
        return file.read_bytes() if file.is_file() else None
    return module_digests(read)


def wheel_digests(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

        def read(path):
            return archive.read(path) if path in names else None
        return module_digests(read)


def _installed_requires():
    """The installed distribution's declared dependencies (what a wheel built
    from this source must declare too), or None when no `bto` distribution
    metadata is installed."""
    try:
        return sorted(importlib.metadata.requires(DISTRIBUTION) or [])
    except importlib.metadata.PackageNotFoundError:
        return None


def _installed_version():
    """The installed distribution's version — the deployment version of the
    source about to submit (pyproject.toml, via the editable install) — or
    None when no `bto` distribution metadata is installed."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def _metadata_field(metadata, field):
    """The first `Field: value` line of a METADATA text, or None."""
    for line in metadata.splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return None


def archive_problems(wheel):
    """Everything in the archive that could change what executes without
    touching the hashed modules: duplicate members (a second copy of a path
    wins on install); members outside the package and its one dist-info
    directory, and `.pth` files anywhere (they run or install code); under
    `bto/` anything but `.py` source — Python imports an extension module
    (`.so`, `.pyd`) in preference to a `.py` of the same stem and executes
    a hash-unchecked `__pycache__` `.pyc` without reading the `.py`; a
    directory named like a module (`bto/x/mod/` beside `bto/x/mod.py`),
    which is imported instead of the module once it holds an `__init__`;
    a console entry point that does not lead to this module's `main`;
    dependencies the source does not declare (pip would install them); and a
    package name or version other than the installed source's — Databricks
    serverless reuses a cached environment for an unchanged package version,
    so changed code must always ship under a new version (deploy/README.md),
    and a wheel still claiming the previous version is refused."""
    problems = []
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        seen = set()
        for name in names:
            if name in seen:
                problems.append(f"{name}: duplicate archive member")
            seen.add(name)
        dist_infos = sorted({m.group(0) for n in names for m in [_DIST_INFO.match(n)] if m})
        for name in names:
            parts = name.split("/")
            for depth in range(1, len(parts)):
                directory = "/".join(parts[:depth])
                if directory + ".py" in seen:
                    problems.append(f"{name}: directory {directory}/ would be "
                                    f"imported instead of {directory}.py")
                    break
            if name.endswith("/"):
                continue
            if name.lower().endswith(".pth"):
                problems.append(f"{name}: unexpected archive member (a .pth "
                                f"file runs at interpreter start-up)")
            elif name.startswith("bto/"):
                if not name.endswith(".py") or "__pycache__" in parts:
                    problems.append(f"{name}: only .py source modules belong "
                                    f"under bto/ (an extension module, bytecode "
                                    f"or data file could be imported instead of "
                                    f"a verified module)")
            elif not _DIST_INFO.match(name):
                problems.append(f"{name}: unexpected archive member (only bto/ "
                                f"modules and one bto-*.dist-info/ belong here)")
        if len(dist_infos) != 1:
            problems.append(f"expected exactly one bto-*.dist-info/ directory, "
                            f"found {len(dist_infos)}")
            return problems
        dist_info = dist_infos[0]
        entry_points = configparser.ConfigParser(interpolation=None)
        entry_points.optionxform = str
        try:
            entry_points.read_string(
                archive.read(dist_info + "entry_points.txt").decode("utf-8"))
            target = entry_points.get("console_scripts", ENTRY_POINT_NAME,
                                      fallback=None)
        except (KeyError, configparser.Error, UnicodeDecodeError):
            target = None
        if target is None or target.strip() != ENTRY_POINT_TARGET:
            problems.append(f"entry point {ENTRY_POINT_NAME} = {target!r}, "
                            f"expected {ENTRY_POINT_TARGET!r}")
        try:
            metadata = archive.read(dist_info + "METADATA").decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            metadata = ""
        expected_version = _installed_version()
        if expected_version is None:
            problems.append(f"no installed {DISTRIBUTION} distribution metadata "
                            f"to check the wheel's version against")
        else:
            found = (_metadata_field(metadata, "Name"),
                     _metadata_field(metadata, "Version"))
            if found != (DISTRIBUTION, expected_version):
                problems.append(
                    f"wheel is {found[0]} {found[1]}, the installed source is "
                    f"{DISTRIBUTION} {expected_version} — changed code ships "
                    f"under a NEW package version (Databricks serverless keeps "
                    f"a cached environment for an unchanged version): bump "
                    f"pyproject.toml, rebuild and redeploy the wheel")
            if dist_info != f"{DISTRIBUTION}-{expected_version}.dist-info/":
                problems.append(f"{dist_info}: dist-info directory is not "
                                f"{DISTRIBUTION}-{expected_version}.dist-info/")
        expected_requires = _installed_requires()
        if expected_requires is None:
            problems.append(f"no installed {DISTRIBUTION} distribution metadata "
                            f"to check the wheel's dependencies against")
        else:
            found_requires = sorted(
                line.split(":", 1)[1].strip() for line in metadata.splitlines()
                if line.startswith("Requires-Dist:"))
            if found_requires != expected_requires:
                problems.append(f"dependencies differ: wheel {found_requires} != "
                                f"source {expected_requires}")
    return problems


def wheel_mismatch(wheel, root=None):
    """Every way the wheel's executable content differs from the source
    about to submit it — empty means identical: the job modules, the
    package initializers, and the archive itself (`archive_problems`).
    Digests only, never contents."""
    expected, found = source_digests(root), wheel_digests(wheel)
    problems = []
    for path in IDENTITY_FILES:
        if expected[path] is None:
            problems.append(f"{path}: missing from the source tree")
        elif found[path] is None:
            problems.append(f"{path}: missing from {Path(wheel).name}")
        elif expected[path] != found[path]:
            problems.append(f"{path}: wheel {found[path][:12]} != source "
                            f"{expected[path][:12]} — rebuild and redeploy "
                            f"the wheel from this source")
    return problems + archive_problems(wheel)


class CanonicalizationFailed(RuntimeError):
    """The job refused to produce a candidate; nothing was written."""


# ---------------------------------------------------------------- source SQL
# Executed verbatim by run.py through the Statement API (with a listing-hash
# predicate per bucket) and by this job through spark.sql (predicate TRUE).
# `snapshot` is always the pinned form `<table> VERSION AS OF N`.

LATEST = "struct(r.started_at, s.run_id)"


def memberships_sql(snapshot, runs, predicate="TRUE"):
    return f"""
        SELECT s.board, s.market, s.board_job_id, s.fingerprint,
               cast(min(r.started_at) AS STRING) AS first_seen,
               max_by(s.advertiser_name, {LATEST}) AS advertiser
        FROM {snapshot} s JOIN {runs} r ON r.run_id = s.run_id
        WHERE s.fingerprint IS NOT NULL AND {predicate}
        GROUP BY s.board, s.market, s.board_job_id, s.fingerprint"""


def transitions_sql(snapshot, runs, predicate="TRUE"):
    return f"""
        WITH ordered AS (
          SELECT s.board, s.market, s.board_job_id, s.fingerprint,
                 lag(s.fingerprint) OVER (
                   PARTITION BY s.board, s.market, s.board_job_id
                   ORDER BY r.started_at, s.run_id) AS previous
          FROM {snapshot} s JOIN {runs} r ON r.run_id = s.run_id
          WHERE s.fingerprint IS NOT NULL AND {predicate})
        SELECT DISTINCT board, market, board_job_id,
               least(previous, fingerprint) AS fp_lo,
               greatest(previous, fingerprint) AS fp_hi
        FROM ordered WHERE previous IS NOT NULL AND previous != fingerprint"""


def documents_sql(snapshot, runs, predicate="TRUE"):
    """One representative observation per wording — title, description and
    signature taken together from the LATEST observation carrying that
    fingerprint (the observation chronology rule)."""
    return f"""
        SELECT fingerprint, rep.title AS title, rep.text AS text,
               rep.signature AS signature
        FROM (SELECT s.fingerprint,
                     max_by(named_struct('title', s.title,
                                         'text', s.description_text,
                                         'signature', s.minhash_signature),
                            {LATEST}) AS rep
              FROM {snapshot} s JOIN {runs} r ON r.run_id = s.run_id
              WHERE s.fingerprint IS NOT NULL AND {predicate}
              GROUP BY s.fingerprint)"""


def never_usable_sql(snapshot, predicate="TRUE"):
    return f"""
        SELECT s.board, s.market, s.board_job_id FROM {snapshot} s
        WHERE {predicate}
        GROUP BY s.board, s.market, s.board_job_id
        HAVING max(CASE WHEN s.fingerprint IS NOT NULL THEN 1 ELSE 0 END) = 0"""


def document_count_sql(snapshot):
    return (f"SELECT count(DISTINCT fingerprint) FROM {snapshot} "
            f"WHERE fingerprint IS NOT NULL")


# --- the representative must be determined by the data ----------------------
#
# `documents_sql` picks the representative with max_by over
# struct(started_at, run_id). Spark does not specify which row max_by returns
# when several share the maximum, so a wording whose maximum-tied
# observations read differently has no defined representative and NEITHER
# build — full or incremental — is well defined on it. That makes these two
# conditions refusals rather than fallbacks: falling back to the full history
# would be falling back to the same ambiguity. The ordering key is
# deliberately untouched; inventing a tie-break would silently change which
# observation the oracle reads.


def tied_representatives_sql(snapshot, runs, predicate="TRUE"):
    """Observations sharing their wording's maximum (started_at, run_id),
    for the wordings whose tied rows do not read identically.

    Raw difference is a cheap pre-filter for the semantic question
    `build_matching_features.ambiguous_representatives` answers: rows with
    identical raw fields cannot differ semantically, so nothing is missed and
    the expensive comparison sees only a handful of wordings."""
    return f"""
        WITH observed AS (
          SELECT s.fingerprint, s.title, s.description_text,
                 s.minhash_signature, struct(r.started_at, s.run_id) AS stamp
          FROM {snapshot} s JOIN {runs} r ON r.run_id = s.run_id
          WHERE s.fingerprint IS NOT NULL AND {predicate}),
        top AS (
          SELECT fingerprint, max(stamp) AS stamp FROM observed
          GROUP BY fingerprint),
        tied AS (
          SELECT o.fingerprint, o.title, o.description_text, o.minhash_signature
          FROM observed o JOIN top t ON t.fingerprint = o.fingerprint
                                    AND o.stamp = t.stamp),
        differing AS (
          SELECT fingerprint FROM tied GROUP BY fingerprint
          HAVING count(DISTINCT sha2(to_json(struct(title, description_text,
                       minhash_signature)), 256)) > 1)
        SELECT tied.fingerprint, tied.title, tied.description_text,
               tied.minhash_signature
        FROM tied JOIN differing ON differing.fingerprint = tied.fingerprint"""


def representative_violations(tied_rows):
    """Why this generation does not determine one representative per wording,
    or an empty list. Pure, so both engines decide identically.

    Scoped to the MAXIMUM-tied observations and nothing wider. A wording whose
    unique latest observation supersedes older ones is determinate however
    much those older ones differ — `documents_sql` reads only the winner — so
    refusing on any historical difference would change frozen full-history
    semantics rather than protect them."""
    ambiguous = features.ambiguous_representatives(tied_rows)
    if not ambiguous:
        return []
    return [f"{len(ambiguous)} wording(s) whose observations tied at the "
            f"maximum (started_at, run_id) disagree on what canonicalization "
            f"reads, e.g. {ambiguous[:3]}"]


def require_determinate_representatives(spark, snapshot, runs):
    """Refuse `snapshot` unless every wording's representative is determined.

    Raises RepresentativeNotDetermined; nothing has been written at this
    point, and no rebuild can help — the full-history algorithm reads the
    same undetermined value."""
    tied = [(r.fingerprint, r.title, r.description_text, r.minhash_signature)
            for r in spark.sql(tied_representatives_sql(snapshot, runs)).collect()]
    violations = representative_violations(tied)
    if violations:
        raise features.RepresentativeNotDetermined(
            f"{snapshot} does not determine its representatives: "
            + "; ".join(violations) + " — nothing written")


# ------------------------------------------------------ per-row functions
# Pure Python, worker-safe: the UDF bodies. Nothing here decides anything —
# every rule is the imported one.

def to_signed(value):
    """The ONE fixed bijection from the unsigned 64-bit shingle id to a
    signed BIGINT: identity below 2^63, else minus 2^64. Equality-preserving,
    lossless; only equality is ever used."""
    return value if value < _TWO_63 else value - _TWO_64


def shingle_array(doc):
    """The wording's five-word shingle SET as a sorted distinct ARRAY<BIGINT>
    (the recipe's BLAKE2b-8 big-endian ids, signed by `to_signed`)."""
    return sorted(to_signed(i) for i in features.shingle_ids(doc))


def prepare_wording(fingerprint, title, text, signature):
    """One usable wording → (fingerprint_ok, shingles, body, stage,
    normalized_title). fingerprint_ok is the worker-environment equivalence
    check: the matching document rebuilt here must hash to the stored
    fingerprint. The body is the description-only normalization used solely
    by the identical-body exception — never the title-bearing document."""
    doc = features.matching_document(title, text)
    if doc is None:
        return False, [], None, None, None
    return (features.fingerprint(doc) == fingerprint,
            shingle_array(doc),
            features.matching_document(None, text),
            features.career_stage(title),
            features.normalized_title(title))


def band_rows(signature):
    """The stored signature's 42 bands of 3 values: (band, v0, v1, v2) for
    positions 0–125; positions 126 and 127 are unused by the locked recipe."""
    if signature is None or len(signature) != SIGNATURE_LENGTH:
        raise CanonicalizationFailed(
            f"signature length {None if signature is None else len(signature)}"
            f" != {SIGNATURE_LENGTH}")
    values = [int(v) for v in signature]
    return [(band, values[start], values[start + 1], values[start + 2])
            for band, start in enumerate(range(0, BANDED_POSITIONS, ROWS_PER_BAND))]


def judge_row(jaccard, routes, continuous, title_a, title_b, same_listing,
              identical_bodies):
    """The thin adapter: primitives in, `judge_pair`'s verdict as a tuple
    (lane, stage_blocked, slot_disjoint, continuity_override, body_override,
    slot_veto). It calls the locked rule function and nothing else."""
    verdict = canonical.judge_pair(
        float(jaccard), set(routes or ()), bool(continuous), title_a, title_b,
        bool(same_listing), bool(identical_bodies))
    return (verdict["lane"], verdict["stage_blocked"], verdict["slot_disjoint"],
            verdict["continuity_override"], verdict["body_override"],
            verdict["slot_veto"])


# ----------------------------------------------------------- the Spark plan

def _types():
    from pyspark.sql import types as T
    wording = T.StructType([
        T.StructField("fingerprint_ok", T.BooleanType()),
        T.StructField("shingles", T.ArrayType(T.LongType())),
        T.StructField("body", T.StringType()),
        T.StructField("stage", T.StringType()),
        T.StructField("normalized_title", T.StringType()),
    ])
    band = T.ArrayType(T.StructType([
        T.StructField("band", T.IntegerType()), T.StructField("v0", T.LongType()),
        T.StructField("v1", T.LongType()), T.StructField("v2", T.LongType())]))
    verdict = T.StructType([
        T.StructField("lane", T.StringType()),
        T.StructField("stage_blocked", T.BooleanType()),
        T.StructField("slot_disjoint", T.BooleanType()),
        T.StructField("continuity_override", T.BooleanType()),
        T.StructField("body_override", T.BooleanType()),
        T.StructField("slot_veto", T.BooleanType()),
    ])
    mapping = T.StructType([
        T.StructField("board", T.StringType(), False),
        T.StructField("market", T.StringType(), False),
        T.StructField("board_job_id", T.StringType(), False),
        T.StructField("fingerprint", T.StringType(), True),
        T.StructField("canonical_job_id", T.StringType(), False),
        T.StructField("canonicalized_at", T.TimestampType(), True),
    ])
    return wording, band, verdict, mapping


def load_snapshot(spark, snapshot, runs):
    """The four source frames, every one read from the pinned snapshot.

    The determinacy guard runs first, because `documents_sql` below resolves
    one representative per wording with max_by and every frame after it
    inherits that choice."""
    require_determinate_representatives(spark, snapshot, runs)
    return {"memberships": spark.sql(memberships_sql(snapshot, runs)),
            "transitions": spark.sql(transitions_sql(snapshot, runs)),
            "documents": spark.sql(documents_sql(snapshot, runs)),
            "never_usable": spark.sql(never_usable_sql(snapshot))}


def canonicalize(spark, frames, dump=None):
    """Spark: wordings, bands, the three routes, same-listing context,
    exact Jaccard, the pair verdict, accepted edges. Driver: components,
    winners, rows, candidate validation. Returns (rows, diagnostics).
    `dump(name, dataframe)` receives every intermediate for parity checks."""
    from pyspark.sql import functions as F
    wording_type, band_type, verdict_type, _ = _types()
    prepare = F.udf(prepare_wording, wording_type)
    bands_of = F.udf(band_rows, band_type)
    judge = F.udf(judge_row, verdict_type)
    norm_advertiser = F.udf(features.normalized_advertiser, "string")
    timings = {}
    started = time.monotonic()

    # --- one row per usable wording, document rebuilt and checked
    docs = frames["documents"]
    # (no .cache(): PERSIST is not supported on serverless compute; every
    # step is pure, so recomputation changes cost only, never results)
    wordings = docs.withColumn("w", prepare("fingerprint", "title", "text",
                                            "signature")) \
        .select("fingerprint", "title", "signature", "w.*")
    bad = wordings.filter(~F.col("fingerprint_ok")).select("fingerprint") \
        .limit(5).collect()
    if bad:
        total = wordings.filter(~F.col("fingerprint_ok")).count()
        raise CanonicalizationFailed(
            f"{total} wording(s) whose rebuilt matching document does not "
            f"hash to the stored fingerprint, e.g. "
            f"{[r.fingerprint for r in bad]} — worker normalization differs "
            f"from the standardizer; nothing written")
    n_wordings = wordings.count()
    timings["wordings_s"] = round(time.monotonic() - started, 1)

    # --- route 1: exact 42 × 3 banding of the stored signature
    bands = wordings.select("fingerprint", F.explode(bands_of("signature"))
                            .alias("b")).select("fingerprint", "b.*")
    lsh = bands.alias("a").join(bands.alias("b"), ["band", "v0", "v1", "v2"]) \
        .where(F.col("a.fingerprint") < F.col("b.fingerprint")) \
        .select(F.col("a.fingerprint").alias("fp_a"),
                F.col("b.fingerprint").alias("fp_b")).distinct()

    # --- route 2: same market + normalized advertiser + normalized title,
    # membership-driven (a wording enters through any qualifying membership)
    memberships = frames["memberships"]
    keyed = memberships.select("market", "fingerprint",
                               norm_advertiser("advertiser").alias("adv")) \
        .join(wordings.select("fingerprint", "normalized_title"), "fingerprint") \
        .where(F.col("adv").isNotNull() & F.col("normalized_title").isNotNull()) \
        .select("market", "adv", "normalized_title", "fingerprint").distinct()
    advertiser_title = keyed.alias("a").join(
        keyed.alias("b"), ["market", "adv", "normalized_title"]) \
        .where(F.col("a.fingerprint") < F.col("b.fingerprint")) \
        .select(F.col("a.fingerprint").alias("fp_a"),
                F.col("b.fingerprint").alias("fp_b")).distinct()

    # --- route 3: observed same-listing transitions with advertiser continuity
    listing_keys = ["board", "market", "board_job_id"]
    adv_of = memberships.select(*listing_keys, "fingerprint",
                                norm_advertiser("advertiser").alias("adv"))
    lo = adv_of.select(*listing_keys, F.col("fingerprint").alias("fp_lo"),
                       F.col("adv").alias("adv_lo"))
    hi = adv_of.select(*listing_keys, F.col("fingerprint").alias("fp_hi"),
                       F.col("adv").alias("adv_hi"))
    history = frames["transitions"].join(lo, listing_keys + ["fp_lo"]) \
        .join(hi, listing_keys + ["fp_hi"]) \
        .select("fp_lo", "fp_hi",
                (F.col("adv_lo").isNull() | F.col("adv_hi").isNull()
                 | (F.col("adv_lo") == F.col("adv_hi"))).alias("continuous")) \
        .groupBy("fp_lo", "fp_hi").agg(F.max("continuous").alias("continuous")) \
        .select(F.col("fp_lo").alias("fp_a"), F.col("fp_hi").alias("fp_b"),
                "continuous")

    # --- same-listing context for the rotating-slot rule (not a route)
    per_listing = memberships.select(*listing_keys, "fingerprint")
    same_listing = per_listing.alias("a").join(per_listing.alias("b"), listing_keys) \
        .where(F.col("a.fingerprint") < F.col("b.fingerprint")) \
        .select(F.col("a.fingerprint").alias("fp_a"),
                F.col("b.fingerprint").alias("fp_b")).distinct() \
        .withColumn("same_listing", F.lit(True))

    # --- the candidate union with route evidence
    routed = lsh.withColumn("route", F.lit("lsh")) \
        .unionByName(advertiser_title.withColumn("route", F.lit("advertiser_title"))) \
        .unionByName(history.select("fp_a", "fp_b").withColumn("route", F.lit("listing_history")))
    candidates = routed.groupBy("fp_a", "fp_b") \
        .agg(F.array_sort(F.collect_set("route")).alias("routes")) \
        .join(history, ["fp_a", "fp_b"], "left") \
        .join(same_listing, ["fp_a", "fp_b"], "left") \
        .withColumn("continuous", F.coalesce("continuous", F.lit(False))) \
        .withColumn("same_listing", F.coalesce("same_listing", F.lit(False)))

    # --- exact Jaccard on the shingle arrays, then the pair verdict
    side_a = wordings.select(F.col("fingerprint").alias("fp_a"),
                             F.col("title").alias("title_a"),
                             F.col("shingles").alias("shingles_a"),
                             F.col("body").alias("body_a"))
    side_b = wordings.select(F.col("fingerprint").alias("fp_b"),
                             F.col("title").alias("title_b"),
                             F.col("shingles").alias("shingles_b"),
                             F.col("body").alias("body_b"))
    inter = F.size(F.array_intersect("shingles_a", "shingles_b"))
    union = F.size("shingles_a") + F.size("shingles_b") - inter
    judged = candidates.join(side_a, "fp_a").join(side_b, "fp_b") \
        .withColumn("inter", inter).withColumn("union", union) \
        .withColumn("jaccard", F.when((F.size("shingles_a") == 0)
                                      | (F.size("shingles_b") == 0), F.lit(0.0))
                    .otherwise(F.col("inter") / F.col("union"))) \
        .withColumn("identical_bodies", F.col("body_a").isNotNull()
                    & (F.col("body_a") == F.col("body_b"))) \
        .withColumn("v", judge("jaccard", "routes", "continuous", "title_a",
                               "title_b", "same_listing", "identical_bodies")) \
        .select("fp_a", "fp_b", "routes", "continuous", "same_listing", "inter",
                "union", "jaccard", "identical_bodies", "v.*")

    counts = {
        "candidates_lsh": lsh.count(),
        "candidates_advertiser_title": advertiser_title.count(),
        "candidates_listing_history": history.count(),
    }
    flags = judged.agg(
        F.count("*").alias("exact_checks"),
        F.sum(F.col("stage_blocked").cast("int")).alias("stage_blocked_pairs"),
        F.sum(F.col("continuity_override").cast("int")).alias("slot_continuity_overrides"),
        F.sum(F.col("body_override").cast("int")).alias("slot_body_overrides"),
        F.sum(F.col("slot_veto").cast("int")).alias("slot_vetoed_pairs"),
    ).collect()[0].asDict()
    counts["candidates_union"] = flags["exact_checks"]
    counts["exact_checks"] = flags["exact_checks"]
    for key in ("stage_blocked_pairs", "slot_continuity_overrides",
                "slot_body_overrides", "slot_vetoed_pairs"):
        if flags[key]:
            counts[key] = int(flags[key])
    timings["candidates_and_verdicts_s"] = round(time.monotonic() - started, 1)
    if dump:
        dump("wordings", wordings.select(
            "fingerprint", "fingerprint_ok", "stage", "normalized_title",
            F.size("shingles").alias("n_shingles"),
            F.sha2(F.concat_ws(",", F.transform("shingles", lambda x: x.cast("string"))), 256)
            .alias("shingle_digest"),
            F.sha2(F.coalesce(F.col("body"), F.lit("")), 256).alias("body_digest")))
        dump("candidates", judged.select(
            "fp_a", "fp_b", F.concat_ws("|", "routes").alias("routes"),
            "continuous", "same_listing", "inter", "union", "jaccard",
            "identical_bodies", "lane", "stage_blocked", "slot_disjoint",
            "continuity_override", "body_override", "slot_veto"))

    # --- Spark → driver: accepted edges and the small graph inputs only
    accepted = [((r.fp_a, r.fp_b), r.jaccard, r.lane) for r in
                judged.where(F.col("lane").isNotNull())
                .select("fp_a", "fp_b", "jaccard", "lane").collect()]
    nodes = {r.fingerprint: r.title for r in
             wordings.select("fingerprint", "title").collect()}
    member_rows = [{"board": r.board, "market": r.market,
                    "board_job_id": r.board_job_id, "fingerprint": r.fingerprint,
                    "first_seen_membership_at": r.first_seen,
                    "advertiser": r.advertiser}
                   for r in memberships.collect()]
    never_usable = [(r.board, r.market, r.board_job_id)
                    for r in frames["never_usable"].collect()]
    collected = {"accepted_edges": len(accepted), "wording_nodes": len(nodes),
                 "memberships": len(member_rows), "never_usable": len(never_usable)}
    timings["collect_s"] = round(time.monotonic() - started, 1)

    # --- driver: the existing guarded union-find, winners, ids, sentinels
    fingerprints = list(dict.fromkeys(m["fingerprint"] for m in member_rows))
    stage_of = {fp: features.career_stage(nodes.get(fp)) for fp in fingerprints}
    component_of, edges, union_counts = canonical.components_from_edges(
        fingerprints, accepted, stage_of)
    rows = canonical.rows_from_components(member_rows, component_of, never_usable)
    diagnostics = dict(canonical.graph_diagnostics(component_of, edges, member_rows),
                       **counts, **union_counts, n_wordings=n_wordings,
                       collected=collected, timings=timings)
    expected_listings = {(m["board"], m["market"], m["board_job_id"])
                         for m in member_rows} | set(never_usable)
    violations = canonical.validate_candidate(
        rows, expected_listings=expected_listings,
        standardized_fingerprints=set(nodes), titles=nodes)
    if violations:
        raise CanonicalizationFailed(
            f"{len(violations)} candidate violation(s): {violations[:5]}")
    if dump:
        dump("components", spark.createDataFrame(
            [(fp, root) for fp, root in component_of.items()],
            "fingerprint string, root string"))
        dump("edges", spark.createDataFrame(
            [(a, b, float(j)) for (a, b), j in edges.items()],
            "fp_a string, fp_b string, jaccard double"))
    return rows, diagnostics


def write_mapping_scratch(spark, scratch, rows):
    """The run-scoped mapping candidate, `canonicalized_at` NULL — run.py
    stamps, validates jointly at N and publishes."""
    _, _, _, mapping_type = _types()
    frame = spark.createDataFrame(
        [(r["board"], r["market"], r["board_job_id"], r["fingerprint"],
          r["canonical_job_id"], None) for r in rows], mapping_type)
    frame.write.format("delta").mode("overwrite").saveAsTable(scratch)


# ------------------------------------------------- incremental extension
#
# Same semantic product, cheaper construction: when the currently published
# mapping is a provable baseline (bound to P, every standardized commit
# above P insert-only, its invariants intact at P), only the evidence the
# P→N batch could have changed is re-judged and only the touched groups are
# rebuilt; every other group's rows are copied forward verbatim. Every
# decision rule lives in extend_canonical_mapping; this section only
# produces its inputs with Spark. Any FullRebuildRequired falls back to the
# full-history `canonicalize` above — never to a guess.

class _SparkQueries:
    """`freshness` reads (DESCRIBE HISTORY, SHOW TBLPROPERTIES, the shared
    invariant SQL) executed through spark.sql instead of the Statement API,
    so the job runs exactly the same checks run.py would."""

    def __init__(self, spark):
        self._spark = spark

    def query(self, statement, parameters=None):
        if parameters:
            raise CanonicalizationFailed(
                "parameterized statements are not used inside the job")
        return [list(row) for row in self._spark.sql(statement).collect()]


def extension_baseline(spark, source, version, mapping):
    """The provable baseline, or FullRebuildRequired.

    Returns (P, mapping pinned at the version these checks read), so every
    later read of the baseline sees exactly the rows validated here. The
    job — not run.py — owns this gate, so there is one implementation of
    the incremental prerequisites. The table PROPERTY cannot be read at a
    pinned version, which is safe under the system's single-writer rule
    (one cleaning run at a time, the assumption behind run-scoped scratch
    names) and is re-proven by run.py's pre/post publication race checks.

    P is the standardized version the mapping was published against, and
    the incremental path is allowed only when every standardized commit in
    (P, N] merely INSERTED observations: an updated or deleted row could
    have changed evidence the baseline already relied on.
    """
    shim = _SparkQueries(spark)
    if not freshness.table_exists(shim, mapping):
        raise extend.FullRebuildRequired("no_baseline_mapping", mapping)
    bound = freshness.bound_standardized_version(shim, mapping)
    if bound is None:
        raise extend.FullRebuildRequired(
            "baseline_binding_missing",
            f"{mapping} has no integer "
            f"{freshness.STANDARDIZED_VERSION_PROPERTY}")
    if not 0 <= bound <= version:
        raise extend.FullRebuildRequired(
            "baseline_version_invalid",
            f"bound {bound} outside 0..{version}")
    commits = [c for c in freshness.commits_above(shim, source, bound)
               if c[0] <= version]
    if len(commits) != version - bound:
        raise extend.FullRebuildRequired(
            "history_incomplete",
            f"{len(commits)} of {version - bound} commits above {bound} "
            f"retained")
    unsafe = [c for c in commits if not freshness.is_insert_only(c[1], *c[2:])]
    if unsafe:
        raise extend.FullRebuildRequired(
            "history_not_insert_only",
            ", ".join(f"v{v} {op}" for v, op, *_ in unsafe[:5]))
    mapping_pinned = pinned(mapping, freshness.current_version(shim, mapping))
    violations = freshness.mapping_invariants(shim, pinned(source, bound),
                                              mapping_pinned)
    if violations:
        raise extend.FullRebuildRequired(
            "baseline_invalid", "; ".join(violations[:3]))
    return bound, mapping_pinned


def observations_sql(snapshot, runs):
    """One row per standardized observation with its chronology and the
    fields the extension judges — usable and unusable rows alike."""
    return f"""
        SELECT s.board, s.market, s.board_job_id, s.fingerprint, s.title,
               s.description_text, s.minhash_signature, s.advertiser_name,
               s.run_id, r.started_at
        FROM {snapshot} s JOIN {runs} r ON r.run_id = s.run_id"""


def load_extension_frames(spark, snapshot_n, snapshot_p, runs, mapping_pinned):
    """The extension's four source frames: both pinned standardized
    generations, the pinned baseline rows, and the never-usable listings
    at N (sentinels are regenerated, never copied).

    Both generations are guarded, and the two failures mean different things.
    N undetermined is fatal: the result being built is not defined, and the
    full-history rebuild would read the same ambiguity, so it must not be
    caught. P undetermined only means the BASELINE cannot be reasoned from —
    the extension would compare against a representative the published
    mapping may never have used — while N itself may be perfectly determinate
    because a later observation superseded the tie. That is a fallback, not a
    refusal: it must not abort a valid full-history build at N."""
    require_determinate_representatives(spark, snapshot_n, runs)
    try:
        require_determinate_representatives(spark, snapshot_p, runs)
    except features.RepresentativeNotDetermined as undetermined:
        raise extend.FullRebuildRequired(
            "baseline_representative_undetermined", str(undetermined)) from None
    return {
        "obs_n": spark.sql(observations_sql(snapshot_n, runs)),
        "obs_p": spark.sql(observations_sql(snapshot_p, runs)),
        "mapping_rows": spark.sql(
            f"SELECT board, market, board_job_id, fingerprint, "
            f"canonical_job_id FROM {mapping_pinned}"),
        "never_usable": spark.sql(never_usable_sql(snapshot_n)),
    }


def extend_on_spark(spark, frames, dump=None):
    """Spark: the P→N batch, the seed evidence, region-restricted candidate
    discovery and pair judgment. Driver: the shared closure and assembly
    rules from extend_canonical_mapping. Returns (rows, diagnostics) or
    raises FullRebuildRequired.

    Every candidate frame below is scoped to the CURRENT region, recomputed
    each closure round. Scoping them to the seeds instead would be the one
    quiet way to get this wrong: a wording enters the region because its
    baseline component did, and its listing may hold no seed at all, so its
    transitions and same-listing context would simply never be generated.
    """
    from pyspark.sql import functions as F
    from pyspark.sql.window import Window
    wording_type, band_type, verdict_type, _ = _types()
    prepare = F.udf(prepare_wording, wording_type)
    bands_of = F.udf(band_rows, band_type)
    judge = F.udf(judge_row, verdict_type)
    norm_adv = F.udf(features.normalized_advertiser, "string")
    norm_title = F.udf(features.normalized_title, "string")
    semantics_of = F.udf(features.representative_digest, "string")
    started = time.monotonic()
    timings = {}

    obs_n, obs_p = frames["obs_n"], frames["obs_p"]
    listing = ["board", "market", "board_job_id"]
    okey = ["run_id", "board", "market", "board_job_id"]
    mkey = listing + ["fingerprint"]
    usable = F.col("fingerprint").isNotNull()
    stamp = F.struct("started_at", "run_id")

    # --- the inserted batch, proven to extend the baseline generation
    n_total, p_total = obs_n.count(), obs_p.count()
    batch = obs_n.join(obs_p.select(*okey), okey, "left_anti")
    batch_total = batch.count()
    if p_total + batch_total != n_total:
        raise extend.FullRebuildRequired(
            "history_not_append_only",
            f"{p_total} baseline + {batch_total} batch != {n_total} rows")
    batch_usable = batch.where(usable)
    batch_usable_total = batch_usable.count()

    latest_p = obs_p.where(usable).groupBy(*listing) \
        .agg(F.max(stamp).alias("latest"))
    stale = batch_usable.join(latest_p, listing) \
        .where(stamp <= F.col("latest")).count()
    if stale:
        raise extend.FullRebuildRequired(
            "out_of_order_observations",
            f"{stale} batch observation(s) precede standardized history")

    # --- known wordings whose representative now reads differently
    fps_p = obs_p.where(usable).select("fingerprint").distinct()
    batch_fps = batch_usable.select("fingerprint").distinct()
    new_fps_frame = batch_fps.join(fps_p, "fingerprint", "left_anti")
    reobserved = batch_fps.join(fps_p, "fingerprint")

    def representatives(frame):
        return frame.where(usable).groupBy("fingerprint").agg(
            F.max_by(F.struct(F.col("title").alias("title"),
                              F.col("description_text").alias("text"),
                              F.col("minhash_signature").alias("signature")),
                     stamp).alias("rep")).select("fingerprint", "rep.*")

    rep_n = representatives(obs_n)
    # Compare the digest, not raw fields: case, whitespace, the title/body
    # split and non-Latin-script content can change while the representative
    # semantics consumed by canonicalization remain equal.
    def digested(frame):
        return frame.withColumn(
            "semantics", semantics_of("title", "text", "signature")) \
            .select("fingerprint", "semantics")

    changed = [r.fingerprint for r in
               digested(representatives(obs_p).join(reobserved, "fingerprint"))
               .alias("p")
               .join(digested(rep_n).alias("n"), "fingerprint")
               .where(F.col("p.semantics") != F.col("n.semantics"))
               .select("fingerprint").collect()]

    # --- membership-level evidence changes (advertiser, new rows)
    def memberships(frame):
        return frame.where(usable).groupBy(*mkey).agg(
            F.min("started_at").cast("string").alias("first_seen"),
            F.max_by("advertiser_name", stamp).alias("advertiser"))

    mem_n, mem_p = memberships(obs_n), memberships(obs_p)
    touched_mem = batch_usable.select(*mkey).distinct()
    drifted = [tuple(row) for row in touched_mem
               .join(mem_p.alias("p"), mkey).join(mem_n.alias("n"), mkey)
               .where(~norm_adv(F.col("p.advertiser"))
                      .eqNullSafe(norm_adv(F.col("n.advertiser"))))
               .select(*mkey).collect()]
    new_mem_frame = touched_mem.join(mem_p.select(*mkey), mkey, "left_anti")
    new_mem = [((b, m, j), fp) for b, m, j, fp in
               new_mem_frame.select(*mkey).collect()]

    base_rows = [{"board": r.board, "market": r.market,
                  "board_job_id": r.board_job_id, "fingerprint": r.fingerprint,
                  "canonical_job_id": r.canonical_job_id}
                 for r in frames["mapping_rows"].collect()]
    component_of, members_map = extend.base_components(base_rows)

    # new transitions on the listings this batch touched: re-observing a
    # wording the listing already carried adds a listing-history candidate
    # (`A → B → C` plus a later `A` nominates `C ↔ A`) without adding any
    # membership, so nothing above would notice it
    batch_listings = batch_usable.select(*listing).distinct()
    order = Window.partitionBy(*listing).orderBy("started_at", "run_id")

    def listing_transitions(frame):
        return frame.where(usable).join(batch_listings, listing) \
            .withColumn("previous", F.lag("fingerprint").over(order)) \
            .where(F.col("previous").isNotNull()
                   & (F.col("previous") != F.col("fingerprint"))) \
            .select(*listing,
                    F.least("previous", "fingerprint").alias("fp_lo"),
                    F.greatest("previous", "fingerprint").alias("fp_hi")) \
            .distinct()

    fresh_transitions = listing_transitions(obs_n) \
        .subtract(listing_transitions(obs_p)).collect()
    endpoints = {fp for row in fresh_transitions
                 for fp in (row.fp_lo, row.fp_hi)}

    new_fps = {row.fingerprint for row in new_fps_frame.collect()}
    seeds_old = sorted(({fp for _listing, fp in new_mem}
                        | {key[3] for key in drifted} | endpoints
                        | set(changed)) - new_fps)
    seeds = new_fps | set(seeds_old)
    total_wordings = rep_n.count()
    if extend.region_too_large(len(seeds), total_wordings):
        raise extend.FullRebuildRequired(
            "seed_region_too_large",
            f"{len(seeds)} of {total_wordings} wordings seeded")
    timings["extension_detectors_s"] = round(time.monotonic() - started, 1)

    # --- candidate discovery and judgment, restricted to the current region
    bands_all = rep_n.select("fingerprint",
                             F.explode(bands_of("signature")).alias("b")) \
        .select("fingerprint", "b.*")
    keyed = mem_n.select("market", "fingerprint",
                         norm_adv("advertiser").alias("adv")) \
        .join(rep_n.select("fingerprint",
                           norm_title("title").alias("ntitle")),
              "fingerprint") \
        .where(F.col("adv").isNotNull() & F.col("ntitle").isNotNull()) \
        .select("market", "adv", "ntitle", "fingerprint").distinct()
    adv_of = mem_n.select(*listing, "fingerprint",
                          norm_adv("advertiser").alias("adv"))
    counts = defaultdict(int)
    last_judged = [None]

    def accepted_touching(region):
        """Every N candidate pair with an endpoint in `region`, judged."""
        region_frame = spark.createDataFrame(sorted(region), "string") \
            .toDF("fingerprint")
        bands_region = bands_all.join(region_frame, "fingerprint")
        lsh = bands_region.alias("a").join(bands_all.alias("b"),
                                           ["band", "v0", "v1", "v2"]) \
            .where(F.col("a.fingerprint") != F.col("b.fingerprint")) \
            .select(F.least("a.fingerprint", "b.fingerprint").alias("fp_a"),
                    F.greatest("a.fingerprint", "b.fingerprint").alias("fp_b")) \
            .distinct()

        keyed_region = keyed.join(region_frame, "fingerprint")
        advertiser_title = keyed_region.alias("a") \
            .join(keyed.alias("b"), ["market", "adv", "ntitle"]) \
            .where(F.col("a.fingerprint") != F.col("b.fingerprint")) \
            .select(F.least("a.fingerprint", "b.fingerprint").alias("fp_a"),
                    F.greatest("a.fingerprint", "b.fingerprint").alias("fp_b")) \
            .distinct()

        # Listings holding ANY region wording, not merely the seeded ones: a
        # wording pulled in with its baseline component may sit on a listing
        # the batch never touched, and its transitions and co-occurrences are
        # still evidence about the pairs being re-judged.
        region_listings = mem_n.join(region_frame, "fingerprint") \
            .select(*listing).distinct() \
            .unionByName(batch_listings).distinct()
        window = Window.partitionBy(*listing).orderBy("started_at", "run_id")
        transitions = obs_n.where(usable).join(region_listings, listing) \
            .withColumn("previous", F.lag("fingerprint").over(window)) \
            .where(F.col("previous").isNotNull()
                   & (F.col("previous") != F.col("fingerprint"))) \
            .select(*listing,
                    F.least("previous", "fingerprint").alias("fp_lo"),
                    F.greatest("previous", "fingerprint").alias("fp_hi")) \
            .distinct()
        lo = adv_of.select(*listing, F.col("fingerprint").alias("fp_lo"),
                           F.col("adv").alias("adv_lo"))
        hi = adv_of.select(*listing, F.col("fingerprint").alias("fp_hi"),
                           F.col("adv").alias("adv_hi"))
        history = transitions.join(lo, listing + ["fp_lo"]) \
            .join(hi, listing + ["fp_hi"]) \
            .select("fp_lo", "fp_hi",
                    (F.col("adv_lo").isNull() | F.col("adv_hi").isNull()
                     | (F.col("adv_lo") == F.col("adv_hi"))).alias("continuous")) \
            .groupBy("fp_lo", "fp_hi") \
            .agg(F.max("continuous").alias("continuous")) \
            .select(F.col("fp_lo").alias("fp_a"),
                    F.col("fp_hi").alias("fp_b"), "continuous")
        region_a = region_frame.withColumnRenamed("fingerprint", "fp_a")
        region_b = region_frame.withColumnRenamed("fingerprint", "fp_b")

        def touching_region(pairs):
            # unionByName, NEVER union: a left-semi join promotes its key to
            # the first column, so the fp_b branch comes back as (fp_b, fp_a)
            # and a positional union would swap the pair. A swapped pair then
            # fails to join back to `history` and the listing-history route
            # vanishes for exactly the pairs whose region endpoint sorts
            # second.
            return pairs.join(region_a, "fp_a", "left_semi").unionByName(
                pairs.join(region_b, "fp_b", "left_semi")).distinct()

        history_region = touching_region(history.select("fp_a", "fp_b")) \
            .join(history, ["fp_a", "fp_b"])

        per_listing = mem_n.join(region_listings, listing) \
            .select(*listing, "fingerprint")
        same_listing = per_listing.alias("a") \
            .join(per_listing.alias("b"), listing) \
            .where(F.col("a.fingerprint") < F.col("b.fingerprint")) \
            .select(F.col("a.fingerprint").alias("fp_a"),
                    F.col("b.fingerprint").alias("fp_b")).distinct() \
            .withColumn("same_listing", F.lit(True))

        routed = lsh.withColumn("route", F.lit("lsh")) \
            .unionByName(advertiser_title.withColumn(
                "route", F.lit("advertiser_title"))) \
            .unionByName(history_region.select("fp_a", "fp_b").withColumn(
                "route", F.lit("listing_history")))
        candidates = routed.groupBy("fp_a", "fp_b") \
            .agg(F.array_sort(F.collect_set("route")).alias("routes")) \
            .join(history_region, ["fp_a", "fp_b"], "left") \
            .join(same_listing, ["fp_a", "fp_b"], "left") \
            .withColumn("continuous", F.coalesce("continuous", F.lit(False))) \
            .withColumn("same_listing",
                        F.coalesce("same_listing", F.lit(False)))

        # The ceiling decides HERE: before the judgment UDF runs and before a
        # single pair reaches the driver. Counting the candidate frame bounds
        # the collect exactly — the two joins below are one-to-one on
        # fingerprint — so a pathological bucket costs one count, never an
        # unbounded collect, and the answer is a clean fallback rather than a
        # truncated region.
        summary = candidates.agg(
            F.count("*").alias("pairs"),
            F.sum(F.col("same_listing").cast("int")).alias("same_listing"),
        ).collect()[0]
        counts["candidates_judged"] += summary["pairs"]
        counts["same_listing_context"] += summary["same_listing"] or 0
        if extend.too_many_candidates(counts["candidates_judged"],
                                      total_wordings):
            raise extend.FullRebuildRequired(
                "candidate_volume_too_large",
                f"{counts['candidates_judged']} candidate pairs over "
                f"{total_wordings} wordings")
        for row in candidates.select(F.explode("routes").alias("route")) \
                .groupBy("route").count().collect():
            counts[f"candidates_{row['route']}"] += row["count"]

        needed = candidates.select(F.col("fp_a").alias("fingerprint")) \
            .union(candidates.select(F.col("fp_b").alias("fingerprint"))) \
            .union(region_frame).distinct()
        wordings = rep_n.join(needed, "fingerprint") \
            .withColumn("w", prepare("fingerprint", "title", "text",
                                     "signature")) \
            .select("fingerprint", "title", "w.*")
        bad = wordings.filter(~F.col("fingerprint_ok")).select("fingerprint") \
            .limit(5).collect()
        if bad:
            raise CanonicalizationFailed(
                f"wording(s) whose rebuilt matching document does not hash to "
                f"the stored fingerprint, e.g. {[r.fingerprint for r in bad]} "
                f"— worker normalization differs from the standardizer; "
                f"nothing written")
        side_a = wordings.select(F.col("fingerprint").alias("fp_a"),
                                 F.col("title").alias("title_a"),
                                 F.col("shingles").alias("shingles_a"),
                                 F.col("body").alias("body_a"))
        side_b = wordings.select(F.col("fingerprint").alias("fp_b"),
                                 F.col("title").alias("title_b"),
                                 F.col("shingles").alias("shingles_b"),
                                 F.col("body").alias("body_b"))
        inter = F.size(F.array_intersect("shingles_a", "shingles_b"))
        union = F.size("shingles_a") + F.size("shingles_b") - inter
        judged = candidates.join(side_a, "fp_a").join(side_b, "fp_b") \
            .withColumn("inter", inter).withColumn("union", union) \
            .withColumn("jaccard", F.when((F.size("shingles_a") == 0)
                                          | (F.size("shingles_b") == 0),
                                          F.lit(0.0))
                        .otherwise(F.col("inter") / F.col("union"))) \
            .withColumn("identical_bodies", F.col("body_a").isNotNull()
                        & (F.col("body_a") == F.col("body_b"))) \
            .withColumn("v", judge("jaccard", "routes", "continuous",
                                   "title_a", "title_b", "same_listing",
                                   "identical_bodies")) \
            .select("fp_a", "fp_b", "jaccard", "v.*")
        accepted = [((r.fp_a, r.fp_b), r.jaccard, r.lane) for r in
                    judged.where(F.col("lane").isNotNull())
                    .select("fp_a", "fp_b", "jaccard", "lane").collect()]
        last_judged[0] = judged
        return accepted

    region, accepted, closure = extend.close_region(
        seeds, component_of, members_map, accepted_touching, total_wordings)
    if extend.region_too_large(len(region), total_wordings):
        raise extend.FullRebuildRequired(
            "closure_region_too_large",
            f"{len(region)} of {total_wordings} wordings in the closed region")
    if dump and last_judged[0] is not None:
        dump("extension_candidates", last_judged[0])
    timings["extension_candidates_s"] = round(time.monotonic() - started, 1)

    diagnostics = dict(
        counts, **closure,
        batch_observations=batch_total,
        batch_usable_observations=batch_usable_total,
        new_fingerprints=len(new_fps),
        seeded_known_fingerprints=len(seeds_old),
        changed_representatives=len(changed),
        drifted_memberships=len(drifted),
        new_memberships=len(new_mem),
        new_transitions=len(fresh_transitions),
        region_fraction=(round(len(region) / total_wordings, 4)
                         if total_wordings else 0.0),
        accepted_region_edges=len(accepted),
        timings=timings)
    return _assemble_extension(spark, frames, accepted, new_fps, region,
                               base_rows, mem_n, rep_n, diagnostics)


def _assemble_extension(spark, frames, accepted, new_fps, region, base_rows,
                        mem_n, rep_n, counts):
    """Collect the region's memberships and titles, then run the shared
    assembly and the full candidate invariants on the driver."""
    memberships_region, titles_region = [], {}
    if region:
        region_frame = spark.createDataFrame(
            sorted(region), "string").toDF("fingerprint")
        memberships_region = [
            {"board": r.board, "market": r.market,
             "board_job_id": r.board_job_id, "fingerprint": r.fingerprint,
             "first_seen_membership_at": r.first_seen,
             "advertiser": r.advertiser}
            for r in mem_n.join(region_frame, "fingerprint").collect()]
        titles_region = {r.fingerprint: r.title for r in
                         rep_n.join(region_frame, "fingerprint")
                         .select("fingerprint", "title").collect()}
    never_usable = [(r.board, r.market, r.board_job_id)
                    for r in frames["never_usable"].collect()]
    rows, assembly = extend.assemble(
        base_rows, accepted, new_fps, region, memberships_region,
        titles_region, never_usable)

    expected_listings = {(m["board"], m["market"], m["board_job_id"])
                         for m in memberships_region} | set(never_usable) | \
        {(r["board"], r["market"], r["board_job_id"]) for r in base_rows}
    all_fps = {r.fingerprint for r in
               rep_n.select("fingerprint").collect()}
    # titles cover the rebuilt wordings; a copied-forward row's group was
    # already proven stage-clean when the baseline was published, and an
    # unknown title classifies as no stage, so this checks exactly the
    # groups this run decided
    violations = canonical.validate_candidate(
        rows, expected_listings=expected_listings,
        standardized_fingerprints=all_fps, titles=titles_region)
    if violations:
        raise extend.FullRebuildRequired(
            "candidate_invariants",
            f"{len(violations)} violation(s): {violations[:3]}")
    usable_rows = sum(1 for r in rows if r["fingerprint"] is not None)
    expected_usable = mem_n.count()
    if usable_rows != expected_usable:
        raise extend.FullRebuildRequired(
            "row_accounting",
            f"{usable_rows} usable rows != {expected_usable} memberships")
    diagnostics = dict(counts, **assembly)
    diagnostics["rows_total"] = len(rows)
    return rows, diagnostics


def build_candidate(spark, source, version, runs, mapping=None, dump=None):
    """The complete candidate mapping for standardized version N.

    ONE decision point: with a baseline mapping, try the incremental
    extension; without one — or on any `FullRebuildRequired` condition —
    build the full history. Both produce the same semantic product, and the
    caller cannot tell them apart except through `mode` and the fallback
    reason in the diagnostics. A `CanonicalizationFailed` is NOT a fallback:
    it means the data or the worker environment is wrong, and the run must
    stop rather than quietly rebuild around it.
    """
    snapshot = pinned(source, version)
    fallback_reason = fallback_detail = None
    if mapping:
        try:
            baseline, mapping_pinned = extension_baseline(
                spark, source, version, mapping)
            log.info("extending %s (bound to version %d) to version %d",
                     mapping, baseline, version)
            frames = load_extension_frames(
                spark, snapshot, pinned(source, baseline), runs,
                mapping_pinned)
            rows, diagnostics = extend_on_spark(spark, frames, dump=dump)
            diagnostics.update(mode="incremental", baseline_version=baseline,
                               version=version)
            return rows, diagnostics
        except extend.FullRebuildRequired as required:
            fallback_reason = required.reason
            fallback_detail = str(required)
            log.warning("incremental canonicalization not provably safe "
                        "(%s) — falling back to the full-history rebuild",
                        fallback_detail)
    rows, diagnostics = canonicalize(spark, load_snapshot(spark, snapshot, runs),
                                     dump=dump)
    diagnostics.update(mode="full_rebuild", version=version)
    if fallback_reason:
        diagnostics.update(fallback_reason=fallback_reason,
                           fallback_detail=fallback_detail)
    return rows, diagnostics


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Databricks job: canonicalize one pinned standardized snapshot")
    parser.add_argument("--source", help="standardized table")
    parser.add_argument("--version", type=int,
                        help="the standardized Delta version N to read")
    parser.add_argument("--runs", help="scrape_runs table")
    parser.add_argument("--scratch",
                        help="run-scoped mapping scratch table to write")
    parser.add_argument("--mapping", default=None,
                        help="the published mapping to extend incrementally "
                             "when that is provably safe; omitted, or any "
                             "unprovable condition, means a full-history "
                             "rebuild")
    parser.add_argument("--dump-prefix", default=None,
                        help="write every intermediate to <prefix>_<name> "
                             "tables for parity checks (development only)")
    parser.add_argument("--smoke", action="store_true",
                        help="report the worker environment and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    if args.smoke:
        import datasketch
        print(DIAGNOSTICS_MARKER + json.dumps({
            "python": sys.version.split()[0], "datasketch": datasketch.__version__,
            "bands": BANDS, "rows_per_band": ROWS_PER_BAND}))
        return 0
    missing = [name for name in ("source", "version", "runs", "scratch")
               if getattr(args, name) is None]
    if missing:
        parser.error("required: " + ", ".join(f"--{m}" for m in missing))
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()
    snapshot = pinned(args.source, args.version)
    log.info("canonicalizing %s → %s", snapshot, args.scratch)

    def dump(name, frame):
        frame.write.format("delta").mode("overwrite") \
            .saveAsTable(f"{args.dump_prefix}_{name}")
    rows, diagnostics = build_candidate(
        spark, args.source, args.version, args.runs, args.mapping,
        dump=dump if args.dump_prefix else None)
    write_mapping_scratch(spark, args.scratch, rows)
    diagnostics["rows_written"] = len(rows)
    print(DIAGNOSTICS_MARKER + json.dumps(diagnostics, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
