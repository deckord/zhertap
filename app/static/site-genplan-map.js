(function () {
  const page = document.querySelector("[data-cabinet-genplan-map-page]");
  const mapNode = document.querySelector("[data-cabinet-genplan-map]");
  if (!page || !mapNode || !window.L) {
    return;
  }

  const reloadButton = document.querySelector("[data-cabinet-genplan-reload]");
  const statusNode = document.querySelector("[data-cabinet-genplan-status]");
  const resultNode = document.querySelector("[data-cabinet-genplan-result]");
  let genplanLayer = null;
  let clickMarker = null;
  let candidateLayer = null;
  let candidatePoints = [];
  let initialPointChecked = false;

  const map = window.L.map(mapNode, {
    center: [48.0, 67.0],
    zoom: 6,
    scrollWheelZoom: true,
  });

  window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap",
  }).addTo(map);

  reloadButton?.addEventListener("click", loadLayers);
  map.on("click", (event) => checkPoint(event.latlng.lat, event.latlng.lng));

  candidatePoints = loadCandidatePoints();
  loadLayers().finally(checkInitialPoint);

  async function loadLayers() {
    setStatus("Загружаю оцифрованные слои...");
    if (genplanLayer) {
      genplanLayer.remove();
      genplanLayer = null;
    }

    try {
      const response = await fetch(page.dataset.layersUrl);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const payload = await response.json();
      genplanLayer = window.L.geoJSON(payload, {
        style: layerStyle,
        pointToLayer: (feature, latlng) =>
          window.L.circleMarker(latlng, layerStyle(feature)),
        onEachFeature,
      }).addTo(map);

      const bounds = genplanLayer.getBounds();
      const count = payload.features?.length || 0;
      if (bounds.isValid()) {
        map.fitBounds(bounds.pad(0.08), { maxZoom: 15 });
      }
      setStatus(
        count
          ? `Загружено слоев: ${count}. Кликните по карте для проверки.`
          : "По этому анализу оцифрованные слои не найдены.",
      );
    } catch (error) {
      setStatus(`Не удалось загрузить слои: ${error.message}`);
    }
  }

  function loadCandidatePoints() {
    const nodes = document.querySelectorAll("[data-candidate-point]");
    if (!nodes.length) {
      return [];
    }
    const points = [];
    candidateLayer = window.L.layerGroup().addTo(map);
    for (const node of nodes) {
      const lat = Number.parseFloat(node.dataset.lat || "");
      const lon = Number.parseFloat(node.dataset.lon || "");
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        continue;
      }
      points.push({
        lat,
        lon,
        rank: node.dataset.rank || "",
        zone: node.dataset.zone || "",
      });
      const marker = window.L.circleMarker([lat, lon], {
        radius: 7,
        color: "#245b8f",
        fillColor: "#2f7fc7",
        fillOpacity: 0.9,
        weight: 2,
      });
      marker.bindTooltip(`#${node.dataset.rank || ""}`, {
        permanent: true,
        direction: "top",
        offset: [0, -8],
      });
      marker.bindPopup(
        `<strong>Точка #${escapeHtml(node.dataset.rank || "")}</strong><br>` +
          `${escapeHtml(node.dataset.zone || "зона не указана")}`,
      );
      marker.on("click", () => checkPoint(lat, lon));
      candidateLayer.addLayer(marker);
    }
    return points;
  }

  async function checkPoint(latitude, longitude) {
    const lat = roundCoord(latitude);
    const lon = roundCoord(longitude);
    if (clickMarker) {
      clickMarker.remove();
    }
    clickMarker = window.L.marker([latitude, longitude], {
      icon: activePointIcon(),
      zIndexOffset: 1000,
    }).addTo(map);
    clickMarker.bindTooltip("Проверяем", {
      permanent: true,
      direction: "right",
      offset: [14, -28],
      className: "cabinet-genplan-active-tooltip",
    });
    renderText(`Проверяю ${lat}, ${lon}...`);

    try {
      const params = new URLSearchParams({ lat, lon });
      const response = await fetch(`${page.dataset.checkUrl}?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      renderCoverage(await response.json(), lat, lon);
    } catch (error) {
      renderText(`Не удалось проверить точку: ${error.message}`);
    }
  }

  function checkInitialPoint() {
    if (initialPointChecked) {
      return;
    }
    const point = initialPointFromUrl() || candidatePoints[0];
    if (!point) {
      return;
    }
    initialPointChecked = true;
    map.setView([point.lat, point.lon], Math.max(map.getZoom(), 17));
    checkPoint(point.lat, point.lon);
    if (point.rank) {
      setStatus(`Загружены слои. Сразу проверяю точку #${point.rank} из заявки.`);
    }
  }

  function initialPointFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const lat = Number.parseFloat(params.get("lat") || "");
    const lon = Number.parseFloat(params.get("lon") || "");
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      return null;
    }
    return {
      lat,
      lon,
      rank: params.get("rank") || "",
      zone: "",
    };
  }

  function renderCoverage(payload, lat, lon) {
    const confidence = Math.round((payload.confidence || 0) * 100);
    const title = `${coverageLabel(payload.coverage_status)}. ${resultLabel(
      payload.result,
    )}. Уверенность ${confidence}%.`;
    const googleUrl = googleMapsPlaceUrl(lat, lon);

    resultNode.replaceChildren();
    const strong = document.createElement("strong");
    strong.textContent = title;
    resultNode.append(strong);

    const coords = document.createElement("p");
    coords.append(`Координаты: ${lat}, ${lon}. `);
    const google = document.createElement("a");
    google.href = googleUrl;
    google.target = "_blank";
    google.rel = "noopener";
    google.textContent = "Открыть в Google";
    coords.append(google);
    resultNode.append(coords);

    const documents = payload.documents || [];
    if (documents.length) {
      const doc = document.createElement("p");
      doc.textContent = `Документ: ${documents[0].title || "не указан"}${
        documents[0].approval_document ? ` · ${documents[0].approval_document}` : ""
      }${documents[0].approval_date ? ` · ${documents[0].approval_date}` : ""}`;
      resultNode.append(doc);
    }

    const intersections = payload.intersections || [];
    if (intersections.length) {
      const list = document.createElement("ul");
      for (const item of intersections) {
        const row = document.createElement("li");
        row.textContent = `${layerKindLabel(item.layer_type)}${
          item.zone_name ? `: ${item.zone_name}` : ""
        }`;
        list.append(row);
      }
      resultNode.append(list);
    }
  }

  function renderText(text) {
    resultNode.replaceChildren();
    resultNode.textContent = text;
  }

  function setStatus(text) {
    if (statusNode) {
      statusNode.textContent = text;
    }
  }

  function onEachFeature(feature, layer) {
    const props = feature.properties || {};
    layer.bindPopup(
      `<strong>${escapeHtml(layerKindLabel(props.layer_kind))}</strong><br>` +
        `${escapeHtml(props.zone_name || "зона не указана")}<br>` +
        `<span>${escapeHtml(props.purpose || "all")} · ${escapeHtml(
          props.trust_level || "",
        )}</span>`,
    );
  }

  function layerStyle(feature) {
    const kind = feature.properties?.layer_kind;
    if (kind === "allowed") {
      return {
        color: "#137a4d",
        fillColor: "#30b36b",
        fillOpacity: 0.22,
        opacity: 0.9,
        weight: 2,
        radius: 7,
      };
    }
    if (kind === "red_line") {
      return {
        color: "#d2382f",
        fillColor: "#d2382f",
        fillOpacity: 0.1,
        opacity: 0.95,
        weight: 3,
        radius: 7,
      };
    }
    return {
      color: "#8a5a08",
      fillColor: "#f2b94b",
      fillOpacity: 0.22,
      opacity: 0.9,
      weight: 2,
      radius: 7,
    };
  }

  function coverageLabel(value) {
    if (value === "AVAILABLE") {
      return "Есть проверенные слои генплана";
    }
    if (value === "SHADOW_ONLY") {
      return "Есть только черновые слои генплана";
    }
    return "Данных по этой точке нет";
  }

  function resultLabel(value) {
    if (value === "POSSIBLE") {
      return "Потенциально можно";
    }
    if (value === "BLOCKED_BY_RESTRICTION") {
      return "Мешает ограничение или красная линия";
    }
    if (value === "NO_ALLOWED_ZONE") {
      return "Разрешенная зона не найдена";
    }
    return "Нужна ручная проверка";
  }

  function layerKindLabel(value) {
    if (value === "allowed") {
      return "Разрешенная зона";
    }
    if (value === "red_line") {
      return "Красная линия";
    }
    if (value === "prohibited") {
      return "Ограничение";
    }
    return value || "Слой";
  }

  function roundCoord(value) {
    return Number(value).toFixed(6);
  }

  function activePointIcon() {
    return window.L.divIcon({
      className: "cabinet-genplan-pin-icon",
      html: '<span class="cabinet-genplan-pin-shape" aria-hidden="true"></span>',
      iconSize: [32, 44],
      iconAnchor: [16, 42],
      tooltipAnchor: [16, -28],
    });
  }

  function googleMapsPlaceUrl(latitude, longitude) {
    const lat = Number(latitude);
    const lon = Number(longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      return "https://www.google.com/maps";
    }
    const label = `${dms(lat, true)} ${dms(lon, false)}`;
    return (
      "https://www.google.com/maps/place/" +
      encodeURIComponent(label) +
      `/@${lat.toFixed(6)},${lon.toFixed(6)},167m/data=!3m1!1e3!4m4!3m3!8m2!3d${lat.toFixed(6)}!4d${lon.toFixed(6)}`
    );
  }

  function dms(value, isLatitude) {
    const absolute = Math.abs(value);
    const degrees = Math.floor(absolute);
    const minutesFloat = (absolute - degrees) * 60;
    const minutes = Math.floor(minutesFloat);
    const seconds = (minutesFloat - minutes) * 60;
    const direction = isLatitude
      ? value >= 0
        ? "N"
        : "S"
      : value >= 0
        ? "E"
        : "W";
    return `${degrees}°${String(minutes).padStart(2, "0")}'${seconds
      .toFixed(1)
      .padStart(4, "0")}"${direction}`;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }
})();
