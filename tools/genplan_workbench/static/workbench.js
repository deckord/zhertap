(() => {
  "use strict";

  const elements = {
    recordSelect: document.getElementById("recordSelect"),
    filterSelect: document.getElementById("filterSelect"),
    pageInput: document.getElementById("pageInput"),
    transformSelect: document.getElementById("transformSelect"),
    roleSelect: document.getElementById("roleSelect"),
    operatorInput: document.getElementById("operatorInput"),
    notesInput: document.getElementById("notesInput"),
    loadButton: document.getElementById("loadButton"),
    prevButton: document.getElementById("prevButton"),
    nextButton: document.getElementById("nextButton"),
    queueStats: document.getElementById("queueStats"),
    recordMeta: document.getElementById("recordMeta"),
    recordLinks: document.getElementById("recordLinks"),
    notice: document.getElementById("notice"),
    sourceImage: document.getElementById("sourceImage"),
    sourceStage: document.getElementById("sourceStage"),
    sourceMarkers: document.getElementById("sourceMarkers"),
    pendingMarker: document.getElementById("pendingMarker"),
    imageDimensions: document.getElementById("imageDimensions"),
    zoomInput: document.getElementById("zoomInput"),
    zoomOutput: document.getElementById("zoomOutput"),
    cancelPairButton: document.getElementById("cancelPairButton"),
    pointsBody: document.getElementById("pointsBody"),
    pointCount: document.getElementById("pointCount"),
    clearButton: document.getElementById("clearButton"),
    metrics: document.getElementById("metrics"),
    saveDraftButton: document.getElementById("saveDraftButton"),
    submitQaButton: document.getElementById("submitQaButton"),
    exportGcps: document.getElementById("exportGcps"),
    exportQa: document.getElementById("exportQa"),
  };

  const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 20,
    attribution: "&copy; OpenStreetMap contributors",
  });
  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {
      maxZoom: 20,
      attribution: "Tiles &copy; Esri",
    },
  );
  const egknBoundaryLayer = L.geoJSON(null, {
    interactive: false,
    style: { color: "#ff00ff", weight: 3, fillOpacity: 0.05, fillColor: "#ff00ff" },
  });
  const egknParcelsLayer = L.geoJSON(null, {
    interactive: false,
    style: { color: "#00c8ff", weight: 1, fillOpacity: 0.08, fillColor: "#00c8ff" },
  });
  const map = L.map("map", { layers: [osm, egknBoundaryLayer] }).setView([48.1, 67.1], 5);
  L.control
    .layers(
      { OSM: osm, "Esri World Imagery": satellite },
      { "EGKN boundary": egknBoundaryLayer, "EGKN parcels": egknParcelsLayer },
    )
    .addTo(map);
  const mapMarkers = L.layerGroup().addTo(map);
  requestAnimationFrame(() => map.invalidateSize());
  window.addEventListener("resize", () => map.invalidateSize());

  const state = {
    records: [],
    filteredRecords: [],
    record: null,
    points: [],
    pendingPixel: null,
    residuals: new Map(),
  };

  function showNotice(message, kind = "") {
    elements.notice.textContent = message;
    elements.notice.className = `notice ${kind}`.trim();
  }

  function displayText(value) {
    const text = value == null ? "" : String(value);
    if (!/[ÐÑÒ]/.test(text)) return text;
    try {
      const bytes = Uint8Array.from([...text], (char) => char.charCodeAt(0) & 0xff);
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return text;
    }
  }

  function queueLabel(status) {
    return {
      manual_georeference_required: "manual GCP",
      pdf_page_selection_required: "PDF page",
      duplicate_manual_file: "duplicate",
      identity_review_required: "identity review",
      proposed: "proposed",
      qa_pending: "QA pending",
    }[status] || status || "unknown";
  }

  function bboxLabel(record) {
    if (!record?.bbox_status) return "";
    if (record.bbox_status === "resolved") {
      const source = {
        egkn: "EGKN boundary",
        static_bbox: "city reference",
        nominatim: "Nominatim",
      }[record.bbox_source] || record.bbox_source || "bbox";
      const label = displayText(record.bbox_label || "");
      return `map area found: ${source}${label ? ` (${label})` : ""}`;
    }
    const reason = displayText(record.bbox_reason || "");
    return `map area needs review${reason ? `: ${reason}` : ""}`;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      throw new Error(payload?.detail || `HTTP ${response.status}`);
    }
    return payload;
  }

  function optionLabel(record) {
    const location = [record.region, record.district, record.locality]
      .map(displayText)
      .filter(Boolean)
      .join(" / ");
    return `${location ? `${location} | ` : ""}${displayText(record.filename)}`;
  }

  async function loadManifest() {
    try {
      const payload = await api("/api/records");
      state.records = payload.records || [];
      renderQueueStats();
      const initialRecordId = new URLSearchParams(window.location.search).get("record");
      if (
        initialRecordId
        && state.records.some((record) => record.record_id === initialRecordId)
      ) {
        elements.filterSelect.value = "all";
        applyFilter(initialRecordId);
        openRecord();
      } else {
        applyFilter();
      }
      if (!state.records.length) {
        showNotice("Manifest has no supported JPG, PNG or PDF records.", "error");
        elements.loadButton.disabled = true;
        return;
      }
      showNotice(`Manifest loaded: ${state.records.length} documents.`);
    } catch (error) {
      showNotice(`Could not load manifest: ${error.message}`, "error");
    }
  }

  function renderQueueStats() {
    const counts = new Map();
    let saved = 0;
    let qaPending = 0;
    let bboxResolved = 0;
    let bboxUnresolved = 0;
    let autoregAttempts = 0;
    let autoregErrors = 0;
    let diagnosticAnchors = 0;
    for (const record of state.records) {
      counts.set(record.queue_status, (counts.get(record.queue_status) || 0) + 1);
      if (record.has_saved_gcps) saved += 1;
      if (record.workflow_status === "qa_pending") qaPending += 1;
      if (record.bbox_status === "resolved") bboxResolved += 1;
      if (record.bbox_status && record.bbox_status !== "resolved") bboxUnresolved += 1;
      if (record.autoreg_has_attempts) autoregAttempts += 1;
      if (record.autoreg_has_pipeline_error) autoregErrors += 1;
      if ((Number(record.autoreg_diagnostic_anchor_count) || 0) > 0) {
        diagnosticAnchors += 1;
      }
    }
    const chips = [
      ["total", state.records.length],
      ["needs work", state.records.filter((record) => !record.has_saved_gcps).length],
      ["saved GCP", saved],
      ["QA pending", qaPending],
      ["map area found", bboxResolved],
      ["map area review", bboxUnresolved],
      ["autoreg checked", autoregAttempts],
      ["autoreg errors", autoregErrors],
      ["diagnostic anchors", diagnosticAnchors],
      ...[...counts.entries()].sort().map(([status, count]) => [queueLabel(status), count]),
    ];
    elements.queueStats.innerHTML = chips
      .map(([label, count]) => `<span class="stat-chip">${label}: <strong>${count}</strong></span>`)
      .join("");
  }

  function recordMatchesFilter(record, filter) {
    if (filter === "all") return true;
    if (filter === "todo") return !record.has_saved_gcps;
    if (filter === "saved") return record.has_saved_gcps;
    if (filter === "qa_pending") return record.workflow_status === "qa_pending";
    if (filter === "bbox_resolved") return record.bbox_status === "resolved";
    if (filter === "bbox_unresolved") {
      return record.bbox_status && record.bbox_status !== "resolved";
    }
    if (filter === "diagnostic_anchors") {
      return (Number(record.autoreg_diagnostic_anchor_count) || 0) > 0;
    }
    if (filter === "autoreg_priority") return record.autoreg_has_attempts;
    return record.queue_status === filter;
  }

  function applyFilter(selectedId = elements.recordSelect.value) {
    const filter = elements.filterSelect.value;
    state.filteredRecords = state.records.filter((record) =>
      recordMatchesFilter(record, filter),
    );
    if (filter === "autoreg_priority" || filter === "diagnostic_anchors") {
      state.filteredRecords.sort(
        (left, right) =>
          (Number(right.autoreg_operator_score) || 0) -
          (Number(left.autoreg_operator_score) || 0),
      );
    }
    elements.recordSelect.innerHTML = "";
    for (const record of state.filteredRecords) {
      const option = document.createElement("option");
      option.value = record.record_id;
      const saved = record.has_saved_gcps ? ` | ${record.saved_point_count} GCP saved` : "";
      const bbox = record.bbox_status === "resolved"
        ? ` | map: ${record.bbox_source || "found"}`
        : record.bbox_status
          ? " | map: review"
          : "";
      const autoreg = record.autoreg_has_attempts
        ? ` | autoreg: ${record.autoreg_best_basemap || "?"} score ${formatNumber(record.autoreg_operator_score)} inliers ${formatNumber(record.autoreg_inliers)} rmse ${formatNumber(record.autoreg_rmse_px)} anchors ${formatNumber(record.autoreg_diagnostic_anchor_count)}`
        : "";
      option.textContent =
        `${queueLabel(record.queue_status)} | ${optionLabel(record)}${bbox}${saved}${autoreg}`;
      elements.recordSelect.append(option);
    }
    if (state.filteredRecords.some((record) => record.record_id === selectedId)) {
      elements.recordSelect.value = selectedId;
    }
    const hasRecords = state.filteredRecords.length > 0;
    elements.loadButton.disabled = !hasRecords;
    elements.prevButton.disabled = !hasRecords;
    elements.nextButton.disabled = !hasRecords;
    if (!hasRecords) {
      showNotice("No records match this filter.", "error");
    }
  }

  function updateRecordUrl(recordId) {
    const url = new URL(window.location.href);
    url.searchParams.set("record", recordId);
    window.history.replaceState(null, "", url);
  }

  function moveSelection(delta) {
    if (!state.filteredRecords.length) return;
    const current = elements.recordSelect.value;
    const currentIndex = state.filteredRecords.findIndex(
      (record) => record.record_id === current,
    );
    const fallback = delta > 0 ? -1 : 0;
    const nextIndex =
      (currentIndex === -1 ? fallback : currentIndex) + delta;
    const wrappedIndex =
      (nextIndex + state.filteredRecords.length) % state.filteredRecords.length;
    elements.recordSelect.value = state.filteredRecords[wrappedIndex].record_id;
    openRecord();
  }

  function setExports(enabled) {
    const recordId = encodeURIComponent(state.record?.record_id || "");
    for (const [element, name] of [
      [elements.exportGcps, "gcps"],
      [elements.exportQa, "qa"],
    ]) {
      element.classList.toggle("disabled", !enabled);
      element.href = enabled ? `/api/records/${recordId}/export/${name}` : "#";
    }
  }

  async function openRecord() {
    const recordId = elements.recordSelect.value;
    if (!recordId) return;
    updateRecordUrl(recordId);
    showNotice("Loading source scan...");
    setExports(false);
    try {
      const record = await api(`/api/records/${encodeURIComponent(recordId)}`);
      state.record = record;
      state.points = record.saved?.points || [];
      state.pendingPixel = null;
      state.residuals.clear();
      elements.pageInput.max = record.page_count || 1;
      elements.pageInput.value = record.saved?.page || 1;
      elements.transformSelect.value = record.saved?.transform_type || "affine";
      elements.operatorInput.value = record.saved?.operator || "";
      elements.notesInput.value = record.saved?.notes || "";
      elements.recordMeta.textContent = [
        record.region,
        record.district,
        record.locality,
        record.filename,
        bboxLabel(record),
      ].map(displayText).filter(Boolean).join("  |  ");
      const queueRecord = state.records.find((item) => item.record_id === recordId);
      if (queueRecord) {
        elements.recordMeta.textContent +=
          `  |  ${queueLabel(queueRecord.queue_status)}  |  saved GCP: ${queueRecord.saved_point_count || 0}`;
      }
      renderRecordLinks(record);
      elements.sourceImage.src =
        `/api/records/${encodeURIComponent(recordId)}/image?page=${elements.pageInput.value}` +
        `&cache=${Date.now()}`;
      renderAll();
      await focusRecordMap(recordId);
      loadEgknOverlays(recordId);
      setExports(Boolean(record.saved?.points?.length));
    } catch (error) {
      showNotice(`Could not open document: ${error.message}`, "error");
    }
  }

  function renderRecordLinks(record) {
    elements.recordLinks.innerHTML = "";
    const recordId = encodeURIComponent(record.record_id);
    if (record.has_contact_sheet) {
      const link = document.createElement("a");
      link.className = "button-link";
      link.href = `/api/records/${recordId}/contact-sheet`;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Open PDF contact sheet";
      elements.recordLinks.append(link);
    }
    renderAutoregLinks(record, recordId);
  }

  function renderAutoregLinks(record, recordId) {
    const diagnostics = record.autoreg_diagnostics || {};
    const attempts = Array.isArray(diagnostics.attempts) ? diagnostics.attempts : [];
    if (!attempts.length) return;
    const best = diagnostics.best_attempt || attempts[0];
    const panel = document.createElement("div");
    panel.className = "autoreg-panel";
    const metrics = best.metrics || {};
    const reasons = Array.isArray(best.reasons) ? best.reasons.slice(0, 4) : [];
    panel.innerHTML = `
      <div>
        <strong>Autoreg diagnostics</strong>
        <span>
          ${displayText(best.basemap || "") || "attempt"} · confidence ${best.confidence ?? 0} ·
          inliers ${metrics.inliers ?? "-"} · RMSE ${metrics.reprojection_rmse_px ?? "-"}
        </span>
        ${reasons.length ? `<small>${reasons.map(displayText).join("; ")}</small>` : ""}
      </div>
    `;
    const actions = document.createElement("div");
    actions.className = "autoreg-actions";
    for (const artifact of ["plan_preview", "basemap", "matches", "result"]) {
      if (!best.artifacts?.[artifact]) continue;
      const link = document.createElement("a");
      link.className = "button-link";
      link.href =
        `/api/records/${recordId}/autoreg/${encodeURIComponent(best.basemap)}/${artifact}`;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = artifact.replace("_", " ");
      actions.append(link);
    }
    if (Number(best.diagnostic_anchor_count) > 0) {
      const button = document.createElement("button");
      button.className = "secondary";
      button.type = "button";
      button.textContent = `Load ${formatNumber(best.diagnostic_anchor_count)} anchors`;
      button.addEventListener("click", () => loadDiagnosticAnchors(best.basemap));
      actions.append(button);
    }
    panel.append(actions);
    elements.recordLinks.append(panel);
  }

  async function loadDiagnosticAnchors(basemap) {
    if (!state.record) {
      showNotice("Open a source document first.", "error");
      return;
    }
    if (!elements.sourceImage.naturalWidth || !elements.sourceImage.naturalHeight) {
      showNotice("Wait until the source image is loaded, then load anchors.", "error");
      return;
    }
    if (
      state.points.length &&
      !window.confirm("Replace current points with diagnostic anchor draft points?")
    ) {
      return;
    }
    try {
      const payload = await api(
        `/api/records/${encodeURIComponent(state.record.record_id)}` +
          `/diagnostic-anchors/${encodeURIComponent(basemap)}`,
      );
      const anchors = Array.isArray(payload.anchors) ? payload.anchors : [];
      if (!anchors.length) {
        showNotice("No diagnostic anchors are available for this attempt.", "error");
        return;
      }
      const scaleX =
        elements.sourceImage.naturalWidth /
        Math.max(1, Number(payload.matcher_image_width_px) || elements.sourceImage.naturalWidth);
      const scaleY =
        elements.sourceImage.naturalHeight /
        Math.max(1, Number(payload.matcher_image_height_px) || elements.sourceImage.naturalHeight);
      state.points = anchors
        .map((anchor, index) => {
          const plan = anchor.plan_pixel || {};
          const lonlat = anchor.reference_lonlat || {};
          const lon = Number(lonlat.longitude);
          const lat = Number(lonlat.latitude);
          const pixelX = Number(plan.x) * scaleX;
          const pixelY = Number(plan.y) * scaleY;
          if (
            !Number.isFinite(pixelX) ||
            !Number.isFinite(pixelY) ||
            !Number.isFinite(lon) ||
            !Number.isFinite(lat)
          ) {
            return null;
          }
          return {
            id: `diagnostic-${crypto.randomUUID()}`,
            pixel_x: Math.max(0, Math.min(elements.sourceImage.naturalWidth, pixelX)),
            pixel_y: Math.max(0, Math.min(elements.sourceImage.naturalHeight, pixelY)),
            lon,
            lat,
            role: "train",
            label: `diagnostic anchor ${index + 1}; verify manually`,
            reference_source: `${basemap} diagnostic only`,
          };
        })
        .filter(Boolean);
      state.pendingPixel = null;
      state.residuals.clear();
      elements.cancelPairButton.disabled = true;
      const guardrails = payload.guardrails || {};
      const summary = payload.summary || {};
      const note = [
        `Diagnostic anchors loaded from ${basemap}.`,
        `Quality: ${summary.quality_label || "unknown"}.`,
        "Operator must verify or adjust every point before sending to QA.",
        guardrails.customer_search_eligible === false
          ? "These anchors are not customer-search eligible."
          : "",
      ].filter(Boolean).join(" ");
      elements.notesInput.value = [elements.notesInput.value, note]
        .filter(Boolean)
        .join("\n");
      renderAll();
      if (state.points.length) {
        const bounds = L.latLngBounds(state.points.map((point) => [point.lat, point.lon]));
        map.fitBounds(bounds.pad(0.15), { maxZoom: 18 });
      }
      showNotice(
        `Loaded ${state.points.length} diagnostic anchors. Verify them manually before saving.`,
        "success",
      );
    } catch (error) {
      showNotice(`Could not load diagnostic anchors: ${error.message}`, "error");
    }
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "0";
    if (Math.abs(number) >= 10) return number.toFixed(0);
    return number.toFixed(2);
  }

  async function focusRecordMap(recordId) {
    if (state.points.length) {
      const bounds = L.latLngBounds(
        state.points.map((point) => [point.lat, point.lon]),
      );
      map.fitBounds(bounds.pad(0.15), { maxZoom: 18 });
      return;
    }
    try {
      const bbox = await api(`/api/records/${encodeURIComponent(recordId)}/bbox`);
      map.fitBounds(
        [
          [bbox.south, bbox.west],
          [bbox.north, bbox.east],
        ],
        { padding: [12, 12] },
      );
    } catch (error) {
      showNotice(
        `The scan opened, but locality boundaries were not found: ${error.message}`,
        "error",
      );
    }
  }

  async function loadEgknOverlays(recordId) {
    egknBoundaryLayer.clearLayers();
    egknParcelsLayer.clearLayers();
    try {
      const boundary = await api(
        `/api/records/${encodeURIComponent(recordId)}/egkn/boundary`,
      );
      egknBoundaryLayer.addData(boundary);
    } catch (error) {
      showNotice(
        `EGKN boundary overlay is unavailable for this record: ${error.message}`,
        "error",
      );
    }
    try {
      const parcels = await api(
        `/api/records/${encodeURIComponent(recordId)}/egkn/parcels`,
      );
      egknParcelsLayer.addData(parcels);
    } catch {
      // Parcels are a bonus overlay; the boundary error above is enough feedback.
    }
  }

  elements.sourceImage.addEventListener("load", () => {
    elements.imageDimensions.textContent =
      `${elements.sourceImage.naturalWidth} x ${elements.sourceImage.naturalHeight} px`;
    setZoom();
    renderImageMarkers();
    map.invalidateSize();
    showNotice(
      "Click a control point on the scan, then click the same location on the map.",
      "success",
    );
  });

  elements.sourceImage.addEventListener("error", async () => {
    let detail = "The server could not render this page.";
    try {
      const response = await fetch(elements.sourceImage.src);
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // Keep the generic rendering error.
    }
    showNotice(detail, "error");
  });

  function sourceCoordinates(event) {
    const rect = elements.sourceImage.getBoundingClientRect();
    return {
      pixel_x:
        ((event.clientX - rect.left) / rect.width) *
        elements.sourceImage.naturalWidth,
      pixel_y:
        ((event.clientY - rect.top) / rect.height) *
        elements.sourceImage.naturalHeight,
    };
  }

  elements.sourceImage.addEventListener("click", (event) => {
    if (!state.record || !elements.sourceImage.naturalWidth) return;
    state.pendingPixel = sourceCoordinates(event);
    elements.cancelPairButton.disabled = false;
    renderPending();
    showNotice("Scan point selected. Now click the matching location on the map.");
  });

  map.on("click", (event) => {
    if (!state.pendingPixel) {
      showNotice("First select a point on the source scan.", "error");
      return;
    }
    state.points.push({
      id: crypto.randomUUID(),
      pixel_x: state.pendingPixel.pixel_x,
      pixel_y: state.pendingPixel.pixel_y,
      lon: event.latlng.lng,
      lat: event.latlng.lat,
      role: elements.roleSelect.value,
      label: "",
      reference_source: map.hasLayer(satellite) ? "Esri World Imagery" : "OSM",
    });
    state.pendingPixel = null;
    elements.cancelPairButton.disabled = true;
    state.residuals.clear();
    renderAll();
    showNotice("Coordinate pair added. Add the next point.");
  });

  function cancelPair() {
    state.pendingPixel = null;
    elements.cancelPairButton.disabled = true;
    renderPending();
    showNotice("Unfinished pair cancelled.");
  }

  function renderPending() {
    if (!state.pendingPixel || !elements.sourceImage.naturalWidth) {
      elements.pendingMarker.hidden = true;
      return;
    }
    elements.pendingMarker.hidden = false;
    elements.pendingMarker.style.setProperty(
      "--pending-x",
      `${(state.pendingPixel.pixel_x / elements.sourceImage.naturalWidth) * 100}%`,
    );
    elements.pendingMarker.style.setProperty(
      "--pending-y",
      `${(state.pendingPixel.pixel_y / elements.sourceImage.naturalHeight) * 100}%`,
    );
  }

  function renderImageMarkers() {
    elements.sourceMarkers.innerHTML = "";
    if (!elements.sourceImage.naturalWidth) return;
    state.points.forEach((point, index) => {
      const marker = document.createElement("div");
      marker.className = `image-marker ${point.role}`;
      marker.textContent = String(index + 1);
      marker.style.left =
        `${(point.pixel_x / elements.sourceImage.naturalWidth) * 100}%`;
      marker.style.top =
        `${(point.pixel_y / elements.sourceImage.naturalHeight) * 100}%`;
      elements.sourceMarkers.append(marker);
    });
    renderPending();
  }

  function mapIcon(point, index) {
    return L.divIcon({
      className: "",
      html: `<span class="map-point ${point.role}">${index + 1}</span>`,
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    });
  }

  function renderMapMarkers() {
    mapMarkers.clearLayers();
    state.points.forEach((point, index) => {
      L.marker([point.lat, point.lon], { icon: mapIcon(point, index) })
        .bindTooltip(`${index + 1}. ${point.role}`)
        .addTo(mapMarkers);
    });
  }

  function focusPoint(index) {
    const point = state.points[index];
    if (!point) return;
    map.setView([point.lat, point.lon], Math.max(map.getZoom(), 17));
    const xRatio = point.pixel_x / elements.sourceImage.naturalWidth;
    const yRatio = point.pixel_y / elements.sourceImage.naturalHeight;
    const viewport = document.getElementById("sourceViewport");
    viewport.scrollTo({
      left: elements.sourceImage.clientWidth * xRatio - viewport.clientWidth / 2,
      top: elements.sourceImage.clientHeight * yRatio - viewport.clientHeight / 2,
      behavior: "smooth",
    });
  }

  function renderTable() {
    elements.pointsBody.innerHTML = "";
    state.points.forEach((point, index) => {
      const row = document.createElement("tr");
      const residual = state.residuals.get(point.id);
      row.innerHTML = `
        <td><button type="button" class="point-link">${index + 1}</button></td>
        <td>
          <select class="role-edit" aria-label="Point role ${index + 1}">
            <option value="train">train</option>
            <option value="checkpoint">checkpoint</option>
          </select>
        </td>
        <td>${point.pixel_x.toFixed(1)} / ${point.pixel_y.toFixed(1)}</td>
        <td>${point.lon.toFixed(7)} / ${point.lat.toFixed(7)}</td>
        <td>${residual === undefined ? "-" : `${residual.toFixed(2)} m`}</td>
        <td><input class="label-edit" maxlength="200" aria-label="Point label ${index + 1}"></td>
        <td><button type="button" class="delete-point" title="Delete point">x</button></td>
      `;
      const role = row.querySelector(".role-edit");
      role.value = point.role;
      role.addEventListener("change", () => {
        point.role = role.value;
        state.residuals.clear();
        renderAll();
      });
      const label = row.querySelector(".label-edit");
      label.value = point.label || "";
      label.addEventListener("input", () => {
        point.label = label.value;
      });
      row.querySelector(".point-link").addEventListener("click", () => focusPoint(index));
      row.querySelector(".delete-point").addEventListener("click", () => {
        state.points.splice(index, 1);
        state.residuals.clear();
        renderAll();
      });
      elements.pointsBody.append(row);
    });
    const trainCount = state.points.filter((point) => point.role === "train").length;
    const checkCount = state.points.length - trainCount;
    elements.pointCount.textContent =
      `${state.points.length} points: ${trainCount} train, ${checkCount} checkpoint`;
  }

  function renderAll() {
    renderImageMarkers();
    renderMapMarkers();
    renderTable();
  }

  function setZoom() {
    const zoom = Number(elements.zoomInput.value);
    elements.zoomOutput.value = `${zoom}%`;
    elements.sourceImage.style.width = `${zoom}%`;
    elements.sourceStage.style.minWidth = `${zoom}%`;
    requestAnimationFrame(renderImageMarkers);
  }

  function payload(status) {
    return {
      page: Number(elements.pageInput.value),
      image_width_px: elements.sourceImage.naturalWidth,
      image_height_px: elements.sourceImage.naturalHeight,
      transform_type: elements.transformSelect.value,
      workflow_status: status,
      operator: elements.operatorInput.value,
      notes: elements.notesInput.value,
      points: state.points,
    };
  }

  function renderMetrics(qa) {
    const calc = qa.calculation;
    state.residuals = new Map(
      calc.point_residuals.map((point) => [point.id, point.residual_m]),
    );
    const rows = [
      ["Workflow", qa.workflow_status],
      ["QA decision", qa.qa_decision],
      ["Train RMSE", calc.train_rmse_m == null ? "-" : `${calc.train_rmse_m} m`],
      [
        "Checkpoint RMSE",
        calc.checkpoint_rmse_m == null ? "no independent checkpoint points" : `${calc.checkpoint_rmse_m} m`,
      ],
      ["Max residual", `${calc.max_residual_m} m`],
      ["Distribution", calc.distribution.status],
      ["Sheet coverage", `${(calc.distribution.coverage_ratio * 100).toFixed(1)}%`],
      ["Suggested accuracy class", calc.suggested_accuracy_class],
      ["Issues", calc.issue_codes.join(", ") || "none"],
      ["Auto approval", "disabled"],
    ];
    elements.metrics.innerHTML = rows
      .map(([name, value]) => `<div><dt>${name}</dt><dd>${value}</dd></div>`)
      .join("");
    renderTable();
  }

  async function save(status) {
    if (!state.record || !elements.sourceImage.naturalWidth) {
      showNotice("Open a source document first.", "error");
      return;
    }
    showNotice("Calculating transform and saving GCPs...");
    try {
      const result = await api(
        `/api/records/${encodeURIComponent(state.record.record_id)}/gcps`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload(status)),
        },
      );
      renderMetrics(result.qa);
      const queueRecord = state.records.find(
        (item) => item.record_id === state.record.record_id,
      );
      if (queueRecord) {
        queueRecord.has_saved_gcps = true;
        queueRecord.saved_point_count = result.gcps.points.length;
        queueRecord.workflow_status = result.gcps.workflow_status;
      }
      renderQueueStats();
      applyFilter(state.record.record_id);
      setExports(true);
      showNotice(
        status === "qa_pending"
          ? "Saved with qa_pending status. A second reviewer is still required."
          : "Proposed draft saved. Auto approval is disabled.",
        "success",
      );
    } catch (error) {
      showNotice(`Could not save: ${error.message}`, "error");
    }
  }

  elements.loadButton.addEventListener("click", openRecord);
  elements.prevButton.addEventListener("click", () => moveSelection(-1));
  elements.nextButton.addEventListener("click", () => moveSelection(1));
  elements.filterSelect.addEventListener("change", () => applyFilter());
  elements.pageInput.addEventListener("change", openRecord);
  elements.zoomInput.addEventListener("input", setZoom);
  elements.cancelPairButton.addEventListener("click", cancelPair);
  elements.clearButton.addEventListener("click", () => {
    if (!state.points.length || window.confirm("Delete all points from this sheet?")) {
      state.points = [];
      state.residuals.clear();
      cancelPair();
      renderAll();
    }
  });
  elements.saveDraftButton.addEventListener("click", () => save("proposed"));
  elements.submitQaButton.addEventListener("click", () => save("qa_pending"));

  loadManifest();
})();
