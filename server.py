"""
TRACE Report Paginated API Server
- Streams all_data.json at startup to build a lightweight metadata index
- Serves paginated report list from in-memory index (~2.5 MB)
- Loads full report detail on-demand from individual files in reports/
- Automatically finds the latest trace_scrape_* output directory
"""

import json
import re
import math
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

import ijson
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── State (populated at startup) ─────────────────────────────────────────────

state: dict[str, Any] = {
    "data_dir": None,       # Path to trace_scrape_* folder
    "summary": {},          # top-level summary block
    "metadata_index": [],   # list of metadata dicts (lightweight)
    "report_id_to_file": {}, # report_id → Path of individual JSON file
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_latest_data_dir() -> Path:
    """Return the most recent trace_scrape_* directory that has all_data.json."""
    base = Path(__file__).parent
    candidates = sorted(base.glob("trace_scrape_*"), reverse=True)
    for d in candidates:
        if d.is_dir() and (d / "all_data.json").exists():
            return d
    raise FileNotFoundError(
        "No trace_scrape_* directory with all_data.json found in "
        f"{base}"
    )


def _safe_report_id(raw_id: str) -> str:
    return re.sub(r"[^\w\-.]", "_", raw_id)


def build_metadata_index(data_dir: Path) -> tuple[list[dict], dict]:
    """
    Stream-parse all_data.json and extract:
      - summary block
      - list of metadata dicts (one per report)
    Returns (metadata_list, summary).
    """
    all_data_path = data_dir / "all_data.json"
    log.info("Streaming metadata index from %s …", all_data_path)

    metadata_list: list[dict] = []
    summary: dict = {}

    with open(all_data_path, "rb") as f:
        parser = ijson.parse(f, use_float=True)

        # We only want  .summary  and  .reports[].metadata
        # ijson prefix for reports[i].metadata.* is  "reports.item.metadata.*"
        current_meta: dict | None = None
        in_summary = False
        summary_buf: dict = {}

        for prefix, event, value in parser:
            # ── summary ──────────────────────────
            if prefix == "summary" and event == "start_map":
                in_summary = True
                summary_buf = {}
                continue
            if in_summary:
                if prefix == "summary" and event == "end_map":
                    summary = summary_buf
                    in_summary = False
                elif event in ("string", "number", "boolean", "null"):
                    key = prefix.split(".")[-1]
                    summary_buf[key] = value
                continue

            # ── reports[].metadata ───────────────
            if prefix == "reports.item.metadata" and event == "start_map":
                current_meta = {}
                continue
            if current_meta is not None:
                if prefix == "reports.item.metadata" and event == "end_map":
                    metadata_list.append(current_meta)
                    current_meta = None
                elif event in ("string", "number", "boolean", "null"):
                    key = prefix.split(".")[-1]
                    current_meta[key] = value

    log.info("Index built: %d reports", len(metadata_list))
    return metadata_list, summary


def build_file_index(data_dir: Path) -> dict[str, Path]:
    """Map report_id → individual JSON file path."""
    reports_dir = data_dir / "reports"
    if not reports_dir.exists():
        log.warning("reports/ sub-directory not found; detail endpoint unavailable")
        return {}
    index: dict[str, Path] = {}
    for f in reports_dir.glob("*.json"):
        # filename is the safe report_id (with underscores)
        index[f.stem] = f
    log.info("File index: %d report files", len(index))
    return index


# ── Rating dimension config ──────────────────────────────────────────────────

TEACHING_QUALITY_QUESTIONS = frozenset([
    "The instructor came to class prepared to teach.",
    "The instructor clearly communicated ideas and information.",
    "The instructor displayed enthusiasm for the course.",
    "The instructor facilitated a respectful and inclusive learning environment.",
    "The instructor used class time effectively.",
])

STUDENT_SUPPORT_QUESTIONS = frozenset([
    "The instructor fairly evaluated my performance.",
    "The instructor provided sufficient feedback.",
    "The instructor was available to assist students outside of class.",
])

LEARNING_IMPACT_QUESTIONS = frozenset([
    "I learned a lot in this course.",
    "In-class sessions were helpful for learning.",
    "Out-of-class assignments and/or fieldwork were helpful for learning.",
    "This course was intellectually challenging.",
])

COURSE_DESIGN_QUESTIONS = frozenset([
    "The syllabus was accurate and helpful in delineating expectations and course outcomes.",
    "Required and additional course materials were helpful in achieving course outcomes.",
])

ONLINE_DELIVERY_QUESTIONS = frozenset([
    "Online course materials were organized to help me navigate through the course week by week.",
    "Online interactions with my instructor created a sense of connection in the virtual classroom.",
    "Online course interactions created a sense of community and connection to my classmates.",
])

DIMENSION_WEIGHTS = {
    "teaching_quality": 0.30,
    "student_support":  0.20,
    "learning_impact":  0.25,
    "course_design":    0.15,
    "online_delivery":  0.10,
}

DIMENSION_LABELS = {
    "teaching_quality": "Teaching Quality",
    "student_support":  "Student Support",
    "learning_impact":  "Learning Impact",
    "course_design":    "Course Design",
    "online_delivery":  "Online Delivery",
}

DIMENSION_QUESTIONS: dict[str, frozenset] = {
    "teaching_quality": TEACHING_QUALITY_QUESTIONS,
    "student_support":  STUDENT_SUPPORT_QUESTIONS,
    "learning_impact":  LEARNING_IMPACT_QUESTIONS,
    "course_design":    COURSE_DESIGN_QUESTIONS,
    "online_delivery":  ONLINE_DELIVERY_QUESTIONS,
}


def _safe_float(value: str | None) -> float | None:
    """Parse a numeric string to float; return None if blank or invalid."""
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _compute_rating(reports: list[dict]) -> dict:
    """
    Aggregate ratings from a list of full report dicts.
    Returns a dict with keys: ratings, overall_rating, vs_dept, vs_univ.
    """
    # Buckets: dim → list of (course_mean, dept_mean, univ_mean)
    buckets: dict[str, list[tuple[float, float | None, float | None]]] = {
        k: [] for k in DIMENSION_QUESTIONS
    }

    for report in reports:
        for row in report.get("ratings", []):
            question = row.get("question", "").strip()
            for dim, question_set in DIMENSION_QUESTIONS.items():
                if question in question_set:
                    cm = _safe_float(row.get("course_mean"))
                    dm = _safe_float(row.get("dept_mean"))
                    um = _safe_float(row.get("univ_mean"))
                    if cm is not None:
                        buckets[dim].append((cm, dm, um))
                    break

    def _avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 2) if vals else None

    ratings: dict[str, dict] = {}
    for dim in DIMENSION_QUESTIONS:
        entries = buckets[dim]
        course_scores = [e[0] for e in entries]
        dept_scores   = [e[1] for e in entries if e[1] is not None]
        univ_scores   = [e[2] for e in entries if e[2] is not None]
        score = _avg(course_scores)
        ratings[dim] = {
            "score":       score,
            "label":       DIMENSION_LABELS[dim],
            "n_questions": len(entries),
            "available":   score is not None,
            "_dept_avg":   _avg(dept_scores),
            "_univ_avg":   _avg(univ_scores),
        }

    # Weighted overall — redistribute weight of absent dimensions proportionally
    active = {d: w for d, w in DIMENSION_WEIGHTS.items() if ratings[d]["available"]}
    overall: float | None = None
    if active:
        total_weight = sum(active.values())
        overall = round(
            sum(ratings[d]["score"] * w / total_weight for d, w in active.items()), 2
        )

    # vs_dept / vs_univ deltas
    vs_dept: dict[str, float | None] = {}
    vs_univ: dict[str, float | None] = {}
    for dim in DIMENSION_QUESTIONS:
        s = ratings[dim]["score"]
        d = ratings[dim]["_dept_avg"]
        u = ratings[dim]["_univ_avg"]
        vs_dept[dim] = round(s - d, 2) if s is not None and d is not None else None
        vs_univ[dim] = round(s - u, 2) if s is not None and u is not None else None

    # Strip internal _dept_avg / _univ_avg from public output
    for dim in ratings:
        del ratings[dim]["_dept_avg"]
        del ratings[dim]["_univ_avg"]

    return {
        "ratings":        ratings,
        "overall_rating": overall,
        "vs_dept":        vs_dept,
        "vs_univ":        vs_univ,
    }


# ── Lifespan (startup / shutdown) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = find_latest_data_dir()
    log.info("Using data directory: %s", data_dir)

    metadata_list, summary = build_metadata_index(data_dir)
    file_index = build_file_index(data_dir)

    state["data_dir"] = data_dir
    state["summary"] = summary
    state["metadata_index"] = metadata_list
    state["report_id_to_file"] = file_index

    yield  # server runs here

    log.info("Server shutting down.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="TRACE Report API",
    description="Paginated access to Northeastern TRACE evaluation reports",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://nubanner.neu.edu"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", summary="Summary statistics")
def root() -> JSONResponse:
    return JSONResponse({
        "data_dir": str(state["data_dir"]),
        "summary": state["summary"],
        "total_indexed": len(state["metadata_index"]),
    })


@app.get("/reports", summary="Paginated full reports (metadata + ratings + comments + demographics + responses)")
def list_reports(
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(20, ge=1, le=100, description="Records per page"),
) -> JSONResponse:
    index = state["metadata_index"]
    file_index = state["report_id_to_file"]
    total = len(index)
    total_pages = max(1, math.ceil(total / limit))

    if page > total_pages:
        raise HTTPException(
            status_code=404,
            detail=f"Page {page} out of range (total pages: {total_pages})",
        )

    start = (page - 1) * limit
    end = start + limit
    page_meta = index[start:end]

    # Load full report for each item in the page slice
    items = []
    for meta in page_meta:
        raw_id = meta.get("report_id", "")
        safe_id = _safe_report_id(raw_id)
        path = file_index.get(safe_id)
        if path:
            with open(path, encoding="utf-8") as f:
                items.append(json.load(f))
        else:
            # Fall back to metadata-only if individual file missing
            items.append({"metadata": meta})

    return JSONResponse({
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "data": items,
    })


@app.get("/reports/{report_id}", summary="Full report detail")
def get_report(report_id: str) -> JSONResponse:
    file_index = state["report_id_to_file"]
    safe_id = _safe_report_id(report_id)

    path = file_index.get(safe_id)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"Report '{report_id}' not found.",
        )

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    return JSONResponse(data)


@app.get("/rating", summary="Computed 5-dimension rating for a course+instructor pair")
def get_rating(
    course_code: str = Query(..., description="Base course code, e.g. AACE6000 (section suffix ignored)"),
    instructor: str  = Query(..., description="Instructor name (case-insensitive substring match)"),
    semester: str | None = Query(None, description="Optional semester filter, e.g. 'Fall 2025'"),
) -> JSONResponse:
    index      = state["metadata_index"]
    file_index = state["report_id_to_file"]

    base_code   = course_code.strip().upper().split("-")[0]
    instr_lower = instructor.strip().replace("+", " ").lower()

    matched_meta: list[dict] = []
    for meta in index:
        # course_code match: strip section suffix (e.g. AACE6000-01 → AACE6000)
        meta_base = meta.get("course_code", "").split("-")[0].upper()
        if meta_base != base_code:
            continue
        # instructor match: case-insensitive substring
        if instr_lower not in meta.get("instructor", "").lower():
            continue
        # optional semester filter
        if semester and meta.get("semester", "") != semester.strip():
            continue
        matched_meta.append(meta)

    if not matched_meta:
        raise HTTPException(
            status_code=404,
            detail=f"No reports found for course '{course_code}' and instructor '{instructor}'."
                   + (f" (semester: {semester})" if semester else ""),
        )

    # Load full reports for matched metadata entries
    reports: list[dict] = []
    for meta in matched_meta:
        raw_id  = meta.get("report_id", "")
        safe_id = _safe_report_id(raw_id)
        path    = file_index.get(safe_id)
        if path:
            with open(path, encoding="utf-8") as f:
                reports.append(json.load(f))
        else:
            reports.append({"metadata": meta, "ratings": []})

    computed = _compute_rating(reports)

    # Aggregate metadata for the response envelope
    semesters       = sorted({m.get("semester", "") for m in matched_meta if m.get("semester")})
    total_responses = sum(
        int(m.get("audience_responses_received", 0) or 0) for m in matched_meta
    )
    course_name = matched_meta[0].get("course_name", "") if matched_meta else ""
    canonical_instructor = matched_meta[0].get("instructor", instructor) if matched_meta else instructor

    return JSONResponse({
        "course_code":   base_code,
        "course_name":   course_name,
        "instructor":    canonical_instructor,
        "matched_reports": len(reports),
        "semesters":     semesters,
        "total_responses": total_responses,
        **computed,
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
