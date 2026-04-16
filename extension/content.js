/**
 * TRACE Ratings for NUBanner — content.js
 * Wrapped in IIFE so all variables are private and can't be clobbered
 * by NUBanner's own globals (e.g. its own `log` variable).
 */
(function TRACE_EXT() {
  "use strict";

  const API_BASE = "https://nutraceblueapi.xyz";

  // Cache: "ACCT1201::Witte Annie" → Promise<rating|null>
  const ratingCache = new Map();

  // Map<lastNameLower, [{subjectCourse, displayName}]>
  const courseData = new Map();

  // ── Logger (no-ops in production; set DEBUG=true locally to enable) ─────────

  const DEBUG = false;

  function tlog(section) {
    if (!DEBUG) return;
    const rest = Array.prototype.slice.call(arguments, 1);
    console.log.apply(console, ["[TRACE-EXT][" + section + "]"].concat(rest));
  }
  function twarn(section) {
    if (!DEBUG) return;
    const rest = Array.prototype.slice.call(arguments, 1);
    console.warn.apply(console, ["[TRACE-EXT][" + section + "]"].concat(rest));
  }
  function terr(section) {
    if (!DEBUG) return;
    const rest = Array.prototype.slice.call(arguments, 1);
    console.error.apply(console, ["[TRACE-EXT][" + section + "]"].concat(rest));
  }

  tlog("INIT", "Content script loaded on", window.location.href);

  // Save original fetch NOW, before we patch it, so fetchRating can bypass our interceptor.
  const originalFetch = window.fetch.bind(window);

  // ── XHR Interception ────────────────────────────────────────────────────────

  (function interceptXHR() {
    const OriginalXHR = window.XMLHttpRequest;
    tlog("XHR", "Patching XMLHttpRequest");

    function PatchedXHR() {
      const xhr = new OriginalXHR();
      let capturedUrl = "";

      const originalOpen = xhr.open.bind(xhr);
      xhr.open = function (method, url) {
        capturedUrl = url;
        tlog("XHR", "open() \u2192 " + method + " " + url);
        return originalOpen.apply(this, arguments);
      };

      xhr.addEventListener("load", function () {
        tlog(
          "XHR",
          "load event, status=" + xhr.status + ", url=" + capturedUrl,
        );
        if (!capturedUrl.includes("searchResults/searchResults")) {
          return;
        }
        tlog("XHR", "searchResults detected! Parsing response...");
        try {
          const json = JSON.parse(xhr.responseText);
          const entries = Array.isArray(json) ? json : json.data || [];
          tlog("XHR", "indexing " + entries.length + " entries");
          indexEntries(entries);
        } catch (e) {
          terr("XHR", "JSON parse failed:", e);
        }
      });

      return xhr;
    }

    Object.setPrototypeOf(PatchedXHR, OriginalXHR);
    PatchedXHR.prototype = OriginalXHR.prototype;
    window.XMLHttpRequest = PatchedXHR;
    tlog("XHR", "XMLHttpRequest patched successfully");
  })();

  // ── Fetch Interception ──────────────────────────────────────────────────────

  (function interceptFetch() {
    // Use the outer originalFetch — no need to rebind here.
    tlog("FETCH", "Patching window.fetch");

    window.fetch = async function (input, init) {
      const url =
        typeof input === "string"
          ? input
          : (input && input.url) || String(input);
      tlog("FETCH", "fetch() \u2192 " + url);

      const response = await originalFetch(input, init);

      if (url.includes("searchResults/searchResults")) {
        tlog(
          "FETCH",
          "searchResults detected in fetch! status=" + response.status,
        );
        response
          .clone()
          .json()
          .then(function (json) {
            const entries = Array.isArray(json) ? json : json.data || [];
            tlog("FETCH", "indexing " + entries.length + " entries from fetch");
            indexEntries(entries);
          })
          .catch(function (e) {
            terr("FETCH", "clone/json failed:", e);
          });
      }

      return response;
    };
    tlog("FETCH", "window.fetch patched successfully");
  })();

  // ── Index Entries ───────────────────────────────────────────────────────────
  //
  // Key: "displayname_lower::coursenum"  e.g. "koppes, abigail::7390"
  // Value: { subjectCourse: "CHME7390", displayName: "Koppes, Abigail" }
  //
  // courseNum is the trailing digits of subjectCourse ("CHME7390" → "7390").
  // This lets processRow do an exact lookup with just the DOM's 4-digit number.

  function indexEntries(entries) {
    let added = 0;
    for (let i = 0; i < entries.length; i++) {
      const entry = entries[i];
      const subjectCourse = entry.subjectCourse;
      if (!subjectCourse) continue;
      // Extract numeric suffix: "CHME7390" → "7390", "ACCT1201" → "1201"
      const courseNum = subjectCourse.replace(/^[A-Za-z]+/, "");
      const faculty = entry.faculty || [];
      for (let j = 0; j < faculty.length; j++) {
        const f = faculty[j];
        if (!f.displayName) continue;
        const key = f.displayName.toLowerCase() + "::" + courseNum;
        courseData.set(key, {
          subjectCourse: subjectCourse,
          displayName: f.displayName,
        });
        added++;
      }
    }
    tlog(
      "INDEX",
      "added " + added + " mappings. courseData.size=" + courseData.size,
    );
    // Re-scan rows that haven't been processed yet (rows that arrived before XHR)
    const unprocessed = document.querySelectorAll(
      "tr:not([data-trace-processed])",
    );
    tlog(
      "INDEX",
      "re-scanning " + unprocessed.length + " unprocessed DOM rows",
    );
    // Iterate NodeList directly — avoids Array allocation
    for (let i = 0; i < unprocessed.length; i++) {
      pendingRows.add(unprocessed[i]);
    }
    scheduleProcessing();
  }

  // ── MutationObserver ────────────────────────────────────────────────────────

  // requestIdleCallback is available in Chrome extensions but polyfill for safety
  const scheduleIdle =
    typeof requestIdleCallback === "function"
      ? function (cb, opts) {
          requestIdleCallback(cb, opts);
        }
      : function (cb) {
          setTimeout(cb, 1);
        };

  const pendingRows = new Set();
  let rafScheduled = false;
  let mutationCount = 0;

  function scheduleProcessing() {
    if (pendingRows.size > 0 && !rafScheduled) {
      rafScheduled = true;
      tlog(
        "OBSERVER",
        "scheduling processRows for " + pendingRows.size + " rows",
      );
      scheduleIdle(processRows, { timeout: 2000 });
    }
  }

  const observer = new MutationObserver(function (mutations) {
    mutationCount++;
    let rowsFound = 0;
    for (let i = 0; i < mutations.length; i++) {
      const addedNodes = mutations[i].addedNodes;
      for (let j = 0; j < addedNodes.length; j++) {
        const node = addedNodes[j];
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        // Avoid Array.from() on NodeList — iterate directly to skip allocation
        if (node.nodeName === "TR") {
          if (!node.dataset.traceProcessed) {
            pendingRows.add(node);
            rowsFound++;
          }
        } else {
          const rows = node.querySelectorAll("tr");
          for (let k = 0; k < rows.length; k++) {
            if (!rows[k].dataset.traceProcessed) {
              pendingRows.add(rows[k]);
              rowsFound++;
            }
          }
        }
      }
    }
    if (rowsFound > 0) {
      tlog(
        "OBSERVER",
        "mutation #" +
          mutationCount +
          ": found " +
          rowsFound +
          " new rows, pending=" +
          pendingRows.size,
      );
    }
    scheduleProcessing();
  });

  function startObserver() {
    observer.observe(document.body, { childList: true, subtree: true });
    tlog("OBSERVER", "MutationObserver active on document.body");
  }

  // document_start runs before <body> exists
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startObserver);
  } else {
    startObserver();
  }

  // ── Row Processing ──────────────────────────────────────────────────────────

  function processRows() {
    rafScheduled = false;
    const rows = Array.from(pendingRows);
    pendingRows.clear();

    tlog(
      "PROCESS",
      "processing " + rows.length + " rows, courseData.size=" + courseData.size,
    );

    // If XHR hasn't fired yet, leave rows unmarked so they get picked up after indexEntries runs
    if (courseData.size === 0) {
      tlog("PROCESS", "courseData empty — deferring until XHR data arrives");
      return;
    }

    let matched = 0;
    let skipped = 0;
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      if (row.dataset.traceProcessed) continue;
      row.dataset.traceProcessed = "1";
      if (processRow(row)) matched++;
      else skipped++;
    }
    tlog(
      "PROCESS",
      "done \u2014 " + matched + " badges injected, " + skipped + " skipped",
    );
  }

  /**
   * For each instructor anchor in the row:
   *  1. Get the 4-digit course number from the DOM (the Course# column)
   *  2. Look up courseData["displayname::coursenum"] for the exact subjectCourse
   *  3. Inject badge
   */
  function processRow(row) {
    const courseNum = getCourseNum(row);
    tlog("ROW", "courseNum from DOM: " + courseNum);

    // Iterate NodeList directly — avoids Array.from() allocation per row
    const anchors = row.querySelectorAll("a");
    tlog("ROW", "anchors found: " + anchors.length);

    let matched = 0;
    for (let i = 0; i < anchors.length; i++) {
      const anchor = anchors[i];
      const text = anchor.textContent.trim();
      if (!text.includes(",")) continue;

      const key = text.toLowerCase() + "::" + courseNum;
      tlog("ROW", 'lookup key: "' + key + '"');

      const entry = courseData.get(key);
      if (!entry) {
        tlog("ROW", 'no entry for key "' + key + '"');
        continue;
      }

      tlog("ROW", "found: " + entry.subjectCourse + ' for "' + text + '"');
      injectBadge(anchor, entry.subjectCourse, entry.displayName);
      matched++;
    }
    return matched > 0;
  }

  /** Extract the 3-4 digit course number from a table row's cells. */
  function getCourseNum(row) {
    // Iterate NodeList directly — avoids Array.from() allocation per row
    const tds = row.querySelectorAll("td");
    for (let i = 0; i < tds.length; i++) {
      const txt = tds[i].textContent.trim();
      if (/^\d{3,4}$/.test(txt)) {
        tlog("GUESS", "courseNum=" + txt + " at td[" + i + "]");
        return txt;
      }
    }
    tlog("GUESS", "no course number found in TDs");
    return null;
  }

  // ── Badge Injection ─────────────────────────────────────────────────────────

  function injectBadge(anchor, courseCode, displayName) {
    const cacheKey = courseCode + "::" + displayName;
    tlog("BADGE", 'injectBadge key="' + cacheKey + '"');

    // Guard: skip if a badge was already injected immediately after this anchor
    const next = anchor.nextElementSibling;
    if (next && next.classList && next.classList.contains("trace-badge")) {
      tlog("BADGE", 'badge already present for "' + cacheKey + '", skipping');
      return;
    }

    if (!ratingCache.has(cacheKey)) {
      ratingCache.set(cacheKey, fetchRating(courseCode, displayName));
    }

    const badge = document.createElement("span");
    badge.className = "trace-badge trace-loading";
    badge.textContent = "\u2026";
    anchor.insertAdjacentElement("afterend", badge);
    tlog("BADGE", "loading placeholder inserted");

    ratingCache.get(cacheKey).then(function (rating) {
      if (!rating) {
        tlog("BADGE", 'no data for "' + cacheKey + '"');
        badge.className = "trace-badge trace-nodata";
        badge.textContent = "No TRACE";
        return;
      }
      tlog(
        "BADGE",
        "rendering score=" + rating.overall_rating + ' for "' + cacheKey + '"',
      );
      renderBadge(badge, rating, displayName);
    });
  }

  function renderBadge(badge, rating, displayName) {
    const score = rating.overall_rating;

    // Guard against malformed API responses
    if (typeof score !== "number" || !isFinite(score)) {
      badge.className = "trace-badge trace-nodata";
      badge.textContent = "No TRACE";
      return;
    }

    const colorClass =
      score >= 4.5 ? "trace-green" : score >= 3.5 ? "trace-amber" : "trace-red";
    badge.className = "trace-badge " + colorClass;
    badge.textContent = score.toFixed(1);

    const tip = buildTooltip(rating, displayName);
    // Attach to body so it's never clipped by table overflow.
    // Store back-reference so orphan cleanup can detect detached badges.
    tip._traceBadge = badge;
    document.body.appendChild(tip);

    badge.addEventListener("mouseenter", function () {
      // Prune tooltips whose badge was removed from the DOM (NUBanner table refreshes)
      var allTips = document.querySelectorAll(".trace-tooltip");
      for (var t = 0; t < allTips.length; t++) {
        if (
          allTips[t]._traceBadge &&
          !document.contains(allTips[t]._traceBadge)
        ) {
          allTips[t].remove();
        }
      }

      const rect = badge.getBoundingClientRect();
      const TIP_WIDTH = 240;
      const TIP_EST_HEIGHT = 290; // generous estimate to handle flip decision
      const MARGIN = 6;

      // Clamp horizontally so tooltip doesn't overflow viewport right edge
      var left = rect.left;
      if (left + TIP_WIDTH + MARGIN > window.innerWidth) {
        left = window.innerWidth - TIP_WIDTH - MARGIN;
      }
      left = Math.max(MARGIN, left);

      // Flip above badge if too close to bottom of viewport
      var top;
      if (
        rect.bottom + TIP_EST_HEIGHT + MARGIN > window.innerHeight &&
        rect.top - TIP_EST_HEIGHT - MARGIN > 0
      ) {
        top = rect.top - TIP_EST_HEIGHT - MARGIN;
      } else {
        top = rect.bottom + MARGIN;
      }

      tip.style.left = left + "px";
      tip.style.top = top + "px";
      tip.classList.add("trace-tip-visible");
    });
    badge.addEventListener("mouseleave", function () {
      tip.classList.remove("trace-tip-visible");
    });
  }

  // ── Tooltip ─────────────────────────────────────────────────────────────────

  const DIM_ORDER = [
    "teaching_quality",
    "student_support",
    "learning_impact",
    "course_design",
    "online_delivery",
  ];

  function buildTooltip(rating, displayName) {
    const score = rating.overall_rating;
    const scoreColor =
      score >= 4.5 ? "#15803d" : score >= 3.5 ? "#b45309" : "#dc2626";

    const tip = document.createElement("div");
    tip.className = "trace-tooltip";

    const header = document.createElement("div");
    header.className = "trace-tip-header";

    const courseSpan = document.createElement("span");
    courseSpan.className = "trace-tip-course";
    courseSpan.textContent = rating.course_code;

    const overallSpan = document.createElement("span");
    overallSpan.className = "trace-tip-overall";
    overallSpan.style.color = scoreColor;
    overallSpan.textContent =
      "\u2605 " +
      (typeof score === "number" && isFinite(score)
        ? score.toFixed(2)
        : "\u2014");

    header.appendChild(courseSpan);
    header.appendChild(overallSpan);
    tip.appendChild(header);

    if (displayName) {
      const instructorDiv = document.createElement("div");
      instructorDiv.className = "trace-tip-instructor";
      const parts = displayName.split(",");
      const formattedName =
        parts.length > 1
          ? parts[1].trim() + " " + parts[0].trim()
          : displayName;
      instructorDiv.textContent = formattedName;
      tip.appendChild(instructorDiv);
    }

    const meta = document.createElement("div");
    meta.className = "trace-tip-meta";
    const reports =
      rating.matched_reports != null && isFinite(rating.matched_reports)
        ? rating.matched_reports
        : 0;
    const responses =
      rating.total_responses != null && isFinite(rating.total_responses)
        ? rating.total_responses
        : 0;
    const evalsWord = reports !== 1 ? "evals" : "eval";
    meta.textContent =
      reports + " " + evalsWord + " \u00b7 " + responses + " responses";
    tip.appendChild(meta);

    const hasDeptDeltas = DIM_ORDER.some(function (dim) {
      return rating.vs_dept && rating.vs_dept[dim] !== undefined;
    });
    if (hasDeptDeltas) {
      const colHeader = document.createElement("div");
      colHeader.className = "trace-dim-col-header";
      const colLeft = document.createElement("span");
      const colRight = document.createElement("span");
      colRight.textContent = "score \u00b7 vs dept";
      colHeader.appendChild(colLeft);
      colHeader.appendChild(colRight);
      tip.appendChild(colHeader);
    }

    for (let i = 0; i < DIM_ORDER.length; i++) {
      const dim = DIM_ORDER[i];
      const r = rating.ratings[dim];
      if (!r || !r.available) continue;

      const row = document.createElement("div");
      row.className = "trace-dim-row";

      const labelSpan = document.createElement("span");
      labelSpan.className = "trace-dim-label";
      labelSpan.textContent = r.label;

      const rightDiv = document.createElement("div");
      rightDiv.className = "trace-dim-right";

      const scoreSpan = document.createElement("span");
      scoreSpan.className = "trace-dim-score";
      scoreSpan.textContent =
        typeof r.score === "number" && isFinite(r.score)
          ? r.score.toFixed(2)
          : "\u2014";
      rightDiv.appendChild(scoreSpan);

      const vsDept = rating.vs_dept && rating.vs_dept[dim];
      if (typeof vsDept === "number" && isFinite(vsDept)) {
        const deltaSpan = document.createElement("span");
        const positive = vsDept >= 0;
        deltaSpan.className =
          "trace-delta " + (positive ? "trace-pos" : "trace-neg");
        deltaSpan.title = "vs. department average";
        deltaSpan.textContent = (positive ? "+" : "") + vsDept.toFixed(2);
        rightDiv.appendChild(deltaSpan);
      }

      row.appendChild(labelSpan);
      row.appendChild(rightDiv);
      tip.appendChild(row);
    }

    return tip;
  }

  // ── API Fetch ────────────────────────────────────────────────────────────────

  async function fetchRating(courseCode, displayName) {
    // NUBanner: "Witte, Annie" → API expects "Annie Witte" (First Last)
    // Guard: names without a comma (e.g. "TBA", "Staff") pass through as-is
    const parts = displayName.split(",");
    const instructorQuery =
      parts.length > 1
        ? parts[1].trim() + " " + parts[0].trim()
        : displayName.trim();
    const url =
      API_BASE +
      "/rating" +
      "?course_code=" +
      encodeURIComponent(courseCode) +
      "&instructor=" +
      encodeURIComponent(instructorQuery);

    tlog("API", "GET " + url);

    try {
      const res = await originalFetch(url);
      tlog(
        "API",
        "response status=" +
          res.status +
          " for " +
          courseCode +
          "/" +
          instructorQuery,
      );
      if (!res.ok) {
        twarn("API", "non-OK: " + res.status);
        return null;
      }
      const data = await res.json();
      tlog(
        "API",
        "got rating: overall=" +
          data.overall_rating +
          ", reports=" +
          data.matched_reports,
      );
      return data;
    } catch (e) {
      terr("API", "fetch failed:", e);
      return null;
    }
  }
})();
