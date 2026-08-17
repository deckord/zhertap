(function () {
  const mapNode = document.querySelector("[data-urban-plan-map]");
  if (!mapNode || !window.L) {
    return;
  }

  const loadButton = document.querySelector("[data-urban-plan-map-load]");
  const statusNode = document.querySelector("[data-urban-plan-map-status]");
  const resultNode = document.querySelector("[data-urban-plan-map-result]");
  const fields = {
    region: document.querySelector("[name='planning_region']"),
    district: document.querySelector("[name='planning_district']"),
    locality: document.querySelector("[name='planning_locality']"),
    requestedUse: document.querySelector("[name='planning_use']"),
    latitude: document.querySelector("[name='planning_lat']"),
    longitude: document.querySelector("[name='planning_lon']"),
    includeShadow: document.querySelector("[name='planning_shadow'][type='checkbox']"),
  };

  let genplanLayer = null;
  let clickMarker = null;
  const map = window.L.map(mapNode, {
    center: initialCenter(),
    zoom: 13,
    scrollWheelZoom: true,
  });

  window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap",
  }).addTo(map);

  loadButton?.addEventListener("click", () => {
    loadLayers();
  });

  map.on("click", (event) => {
    checkPoint(event.latlng.lat, event.latlng.lng);
  });

  loadLayers();

  function initialCenter() {
    const lat = Number.parseFloat(fields.latitude?.value || "");
    const lon = Number.parseFloat(fields.longitude?.value || "");
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      return [lat, lon];
    }
    return [48.0, 67.0];
  }

  function scopeParams() {
    const params = new URLSearchParams();
    params.set("region", fields.region?.value || "");
    params.set("district", fields.district?.value || "");
    params.set("locality", fields.locality?.value || "");
    params.set("requested_use", fields.requestedUse?.value || "");
    params.set("include_shadow", fields.includeShadow?.checked ? "true" : "false");
    return params;
  }

  async function loadLayers() {
    setStatus("Загружаю слои генплана...");
    if (genplanLayer) {
      genplanLayer.remove();
      genplanLayer = null;
    }

    try {
      const response = await fetch(`${mapNode.dataset.layersUrl}?${scopeParams().toString()}`);
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

      const count = payload.features?.length || 0;
      if (!count) {
        setStatus("По выбранной территории слои не найдены.");
        return;
      }
      const bounds = genplanLayer.getBounds();
      if (bounds.isValid()) {
        map.fitBounds(bounds.pad(0.08), { maxZoom: 15 });
      }
      setStatus(`Загружено слоев: ${count}. Нажмите на карту для проверки точки.`);
    } catch (error) {
      setStatus(`Не удалось загрузить слои: ${error.message}`);
    }
  }

  async function checkPoint(latitude, longitude) {
    const lat = roundCoord(latitude);
    const lon = roundCoord(longitude);
    if (fields.latitude) {
      fields.latitude.value = lat;
    }
    if (fields.longitude) {
      fields.longitude.value = lon;
    }
    if (clickMarker) {
      clickMarker.remove();
    }
    clickMarker = window.L.marker([latitude, longitude]).addTo(map);
    renderResult(`Проверяю ${lat}, ${lon}...`);

    const params = scopeParams();
    params.set("lat", lat);
    params.set("lon", lon);
    try {
      const response = await fetch(`${mapNode.dataset.checkUrl}?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      renderCoverage(await response.json(), lat, lon);
    } catch (error) {
      renderResult(`Не удалось проверить точку: ${error.message}`);
    }
  }

  function renderCoverage(payload, lat, lon) {
    const status = coverageLabel(payload.coverage_status);
    const result = resultLabel(payload.result);
    const confidence = Math.round((payload.confidence || 0) * 100);
    const intersections = payload.intersections || [];
    const documents = payload.documents || [];
    const googleUrl =
      "https://www.google.com/maps/search/?api=1&query=" +
      encodeURIComponent(`${lat},${lon}`);

    resultNode.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = `${status}. Результат: ${result}. Уверенность ${confidence}%.`;
    resultNode.append(title);

    const coords = document.createElement("p");
    coords.append(`Координаты: ${lat}, ${lon}. `);
    const link = document.createElement("a");
    link.href = googleUrl;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Открыть Google";
    coords.append(link);
    resultNode.append(coords);

    if (documents.length) {
      const doc = document.createElement("p");
      doc.textContent = `Документ: ${documents[0].title || "не указан"}${
        documents[0].approval_document ? ` · ${documents[0].approval_document}` : ""
      }${documents[0].approval_date ? ` · ${documents[0].approval_date}` : ""}`;
      resultNode.append(doc);
    }

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

  function renderResult(text) {
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
      return "Есть проверенные слои";
    }
    if (value === "SHADOW_ONLY") {
      return "Есть только черновые слои";
    }
    return "Данных по точке нет";
  }

  function resultLabel(value) {
    if (value === "POSSIBLE") {
      return "потенциально можно";
    }
    if (value === "BLOCKED_BY_RESTRICTION") {
      return "мешает ограничение или красная линия";
    }
    if (value === "NO_ALLOWED_ZONE") {
      return "разрешенная зона не найдена";
    }
    return "нужна ручная проверка";
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

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }
})();
