(function () {
  const dataEl = document.getElementById("auction-v2-map-data");
  const egknLayerDataEl = document.getElementById("auction-v2-egkn-layer-data");
  const mapEl = document.querySelector("[data-auction-v2-map]");
  const emptyEl = document.querySelector("[data-auction-v2-empty]");
  const cardEl = document.querySelector("[data-auction-v2-card]");
  const layerInputs = Array.from(document.querySelectorAll("[data-auction-v2-layer]"));
  const egknLayerInputs = Array.from(
    document.querySelectorAll("[data-auction-v2-egkn-layer]")
  );
  const boundaryInput = document.querySelector("[data-auction-v2-boundary-toggle]");
  const fitButton = document.querySelector("[data-auction-v2-fit]");

  if (!dataEl || !mapEl || !cardEl) {
    return;
  }

  const parseJson = (el, fallback) => {
    try {
      return JSON.parse(el ? el.textContent || "[]" : "[]");
    } catch (_error) {
      return fallback;
    }
  };

  const markers = parseJson(dataEl, []).filter((item) => {
    const lat = Number(item.latitude);
    const lon = Number(item.longitude);
    return Number.isFinite(lat) && Number.isFinite(lon);
  });
  const egknLayers = parseJson(egknLayerDataEl, []);
  let selectedId = markers[0] ? markers[0].id : "";
  let markerLayer = null;
  let boundaryLayer = null;
  let contextLayer = null;
  let map = null;

  const text = {
    noLot: "\u041b\u043e\u0442 \u043d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d",
    markersOnMap: "\u043c\u0430\u0440\u043a\u0435\u0440\u043e\u0432 \u043d\u0430 \u043a\u0430\u0440\u0442\u0435",
    mapFallback:
      "\u041a\u0430\u0440\u0442\u0430 \u043d\u0435 \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u043b\u0430\u0441\u044c: Leaflet \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d.",
    regionMissing: "\u0420\u0435\u0433\u0438\u043e\u043d \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d",
    onlyWithCoordinates:
      "\u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u043d\u0430 \u043c\u0430\u0440\u043a\u0435\u0440, \u0447\u0442\u043e\u0431\u044b \u0443\u0432\u0438\u0434\u0435\u0442\u044c \u0446\u0435\u043d\u0443, \u0441\u0440\u043e\u043a, \u043a\u0430\u0434\u0430\u0441\u0442\u0440 \u0438 \u0441\u0441\u044b\u043b\u043a\u0438 \u0434\u043b\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438.",
    risk: "\u0420\u0438\u0441\u043a",
    deadline: "\u0421\u0440\u043e\u043a",
    stage: "\u0421\u0442\u0430\u0434\u0438\u044f",
    start: "\u0421\u0442\u0430\u0440\u0442",
    perSotka: "\u0417\u0430 \u0441\u043e\u0442\u043a\u0443",
    area: "\u041f\u043b\u043e\u0449\u0430\u0434\u044c",
    cadastre: "\u041a\u0430\u0434\u0430\u0441\u0442\u0440",
    card: "\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430",
    egkn: "\u0415\u0413\u041a\u041d",
    osm: "OSM",
    satellite: "\u0421\u043f\u0443\u0442\u043d\u0438\u043a",
    lots: "\u041b\u043e\u0442\u044b",
    boundaries: "\u0413\u0440\u0430\u043d\u0438\u0446\u044b \u0415\u0413\u041a\u041d",
    context: "\u0421\u043b\u043e\u0438 \u0415\u0413\u041a\u041d",
    object: "\u043e\u0431\u044a\u0435\u043a\u0442",
  };

  const safeUrl = (value) => {
    const url = String(value || "").trim();
    if (!url) {
      return "";
    }
    if (url.startsWith("/cabinet/") || /^https?:\/\//i.test(url)) {
      return url;
    }
    return "";
  };

  const escapeHtml = (value) =>
    String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");

  const selectedValues = (inputs) =>
    new Set(inputs.filter((input) => input.checked).map((input) => input.value));

  const activeScopes = () => selectedValues(layerInputs);
  const activeEgknLayers = () => selectedValues(egknLayerInputs);

  const visibleMarkers = () => {
    const scopes = activeScopes();
    return markers.filter((item) => scopes.has(item.scope));
  };

  const textNode = (tag, value, className) => {
    const el = document.createElement(tag);
    if (className) {
      el.className = className;
    }
    el.textContent = value || "-";
    return el;
  };

  const detailRow = (label, value) => {
    const row = document.createElement("div");
    row.className = "auction-v2-map-card-row";
    row.append(textNode("span", label), textNode("strong", value));
    return row;
  };

  const cardLink = (label, url, primary) => {
    const safe = safeUrl(url);
    if (!safe) {
      return null;
    }
    const link = document.createElement("a");
    link.className = primary ? "primary-action compact" : "secondary-action compact";
    link.href = safe;
    link.textContent = label;
    if (!safe.startsWith("/")) {
      link.target = "_blank";
      link.rel = "noopener";
    }
    return link;
  };

  const renderCard = (item) => {
    cardEl.classList.add("is-updating");
    cardEl.replaceChildren();
    if (!item) {
      cardEl.append(
        textNode("span", text.noLot),
        textNode("strong", `${markers.length} ${text.markersOnMap}`),
        textNode("p", text.onlyWithCoordinates)
      );
      window.requestAnimationFrame(() => cardEl.classList.remove("is-updating"));
      return;
    }
    cardEl.append(
      textNode("span", item.scope_label),
      textNode("strong", `${item.score}/100 - ${item.title}`),
      textNode(
        "p",
        [item.region, item.district, item.locality].filter(Boolean).join(" · ") ||
          text.regionMissing
      ),
      detailRow(text.risk, item.risk_label),
      detailRow(text.deadline, item.deadline_label),
      detailRow(text.stage, item.stage_label),
      detailRow(text.start, item.price_text),
      detailRow(text.perSotka, item.price_per_sotka_text),
      detailRow(text.area, item.area_text),
      detailRow(text.cadastre, item.cadastre || "-")
    );
    const actions = document.createElement("div");
    actions.className = "auction-v2-map-card-actions";
    [
      cardLink(text.card, item.url, true),
      cardLink("Google", item.google_maps_url, false),
      cardLink(text.osm, item.osm_map_url, false),
      cardLink(text.egkn, item.egkn_url, false),
      cardLink("E-Qazyna", item.source_url, false),
    ]
      .filter(Boolean)
      .forEach((link) => actions.append(link));
    cardEl.append(actions);
    window.requestAnimationFrame(() => cardEl.classList.remove("is-updating"));
  };

  const geometryStyle = (item, selected) => {
    const risk = item.risk || "low";
    const palette = {
      low: ["#117249", "rgba(17, 114, 73, .14)"],
      medium: ["#b78b2f", "rgba(183, 139, 47, .15)"],
      high: ["#a13a32", "rgba(161, 58, 50, .15)"],
      unknown: ["#425466", "rgba(66, 84, 102, .12)"],
    };
    const [color, fillColor] = palette[risk] || palette.unknown;
    return {
      color,
      fillColor,
      weight: selected ? 4 : 2,
      opacity: item.scope === "archive" ? 0.65 : 0.9,
      fillOpacity: selected ? 0.28 : 0.14,
      dashArray: item.scope === "archive" ? "7 5" : "",
    };
  };

  const contextStyle = (feature) => {
    const styles = {
      free_lands: ["#117249", "rgba(17, 114, 73, .12)", ""],
      pdp: ["#b78b2f", "rgba(183, 139, 47, .12)", "8 5"],
      functional_zones: ["#255f9f", "rgba(37, 95, 159, .12)", ""],
      engineering: ["#0d5d67", "rgba(13, 93, 103, .1)", "3 5"],
    };
    const [color, fillColor, dashArray] =
      styles[feature.layer_code] || ["#425466", "rgba(66, 84, 102, .1)", "5 5"];
    return {
      color,
      fillColor,
      weight: 2,
      opacity: 0.72,
      fillOpacity: 0.14,
      dashArray,
    };
  };

  const pointLayer = (latLng, style) =>
    window.L.circleMarker(latLng, {
      ...style,
      radius: 7,
      weight: 2,
      fillOpacity: 0.7,
    });

  const addGeometry = (group, geometry, options) => {
    if (!geometry || !geometry.type || !Array.isArray(geometry.coordinates)) {
      return null;
    }
    try {
      const layer = window.L.geoJSON(geometry, options);
      layer.addTo(group);
      return layer;
    } catch (_error) {
      return null;
    }
  };

  const selectLot = (item, panToMarker) => {
    selectedId = item ? item.id : "";
    renderAll();
    if (panToMarker && item) {
      map.panTo([Number(item.latitude), Number(item.longitude)], { animate: true });
    }
  };

  const renderLots = (items) => {
    markerLayer.clearLayers();
    boundaryLayer.clearLayers();
    contextLayer.clearLayers();
    const selectedLayerCodes = activeEgknLayers();
    const visibleLotIds = new Set(items.map((item) => item.id));

    items.forEach((item) => {
      const isSelected = item.id === selectedId;
      const icon = window.L.divIcon({
        className: `auction-v2-leaflet-marker risk-${item.risk || "unknown"} scope-${
          item.scope || "active"
        }${isSelected ? " is-selected" : ""}`,
        html: `<span>${escapeHtml(item.score)}</span>`,
        iconSize: [40, 40],
        iconAnchor: [20, 20],
      });
      const marker = window.L.marker([Number(item.latitude), Number(item.longitude)], {
        icon,
        keyboard: true,
        title: item.title || "",
      });
      marker.bindTooltip(
        `<strong>${escapeHtml(item.score)}/100</strong> ${escapeHtml(item.title)}`,
        { direction: "top", opacity: 0.95 }
      );
      marker.on("click", () => selectLot(item, false));
      marker.addTo(markerLayer);

      if (boundaryInput && boundaryInput.checked && item.boundary) {
        const boundary = addGeometry(boundaryLayer, item.boundary, {
          style: geometryStyle(item, isSelected),
        });
        if (boundary) {
          boundary.on("click", () => selectLot(item, false));
        }
      }
    });

    egknLayers
      .filter(
        (feature) =>
          visibleLotIds.has(feature.lot_id) && selectedLayerCodes.has(feature.layer_code)
      )
      .forEach((feature) => {
        const style = contextStyle(feature);
        const layer = addGeometry(contextLayer, feature.geometry, {
          style,
          pointToLayer: (_feature, latLng) => pointLayer(latLng, style),
        });
        if (!layer) {
          return;
        }
        layer.bindTooltip(
          `${escapeHtml(feature.layer_label || text.egkn)}: ${escapeHtml(
            feature.feature_label || text.object
          )}`,
          { direction: "top", opacity: 0.9 }
        );
      });
  };

  const fitToItems = (items) => {
    if (!map) {
      return;
    }
    if (!items.length) {
      map.setView([48.0196, 66.9237], 5);
      return;
    }
    const bounds = window.L.latLngBounds(
      items.map((item) => [Number(item.latitude), Number(item.longitude)])
    );
    items.forEach((item) => {
      if (!item.boundary) {
        return;
      }
      try {
        const geometry = window.L.geoJSON(item.boundary);
        const geometryBounds = geometry.getBounds();
        if (geometryBounds.isValid()) {
          bounds.extend(geometryBounds);
        }
      } catch (_error) {
        // Ignore malformed external geometry; the marker still gives the lot location.
      }
    });
    map.fitBounds(bounds.pad(0.18), { maxZoom: 15 });
  };

  const renderAll = () => {
    const items = visibleMarkers();
    if (!markerLayer || !boundaryLayer || !contextLayer) {
      renderCard(items[0]);
      return;
    }
    if (!items.some((item) => item.id === selectedId)) {
      selectedId = items[0] ? items[0].id : "";
    }
    if (emptyEl) {
      emptyEl.hidden = items.length > 0;
    }
    renderLots(items);
    renderCard(items.find((item) => item.id === selectedId));
  };

  const bootLeaflet = () => {
    if (!window.L) {
      mapEl.classList.add("auction-v2-map-fallback");
      mapEl.textContent = text.mapFallback;
      renderCard(markers[0]);
      return;
    }

    map = window.L.map(mapEl, {
      zoomControl: true,
      scrollWheelZoom: true,
      preferCanvas: true,
    }).setView([48.0196, 66.9237], 5);
    mapEl.classList.add("is-ready");

    const osm = window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap",
    });
    const satellite = window.L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      {
        maxZoom: 19,
        attribution: "Tiles &copy; Esri",
      }
    );
    osm.addTo(map);

    markerLayer = window.L.layerGroup().addTo(map);
    boundaryLayer = window.L.layerGroup().addTo(map);
    contextLayer = window.L.layerGroup().addTo(map);
    window.L.control
      .layers(
        { OpenStreetMap: osm, [text.satellite]: satellite },
        {
          [text.lots]: markerLayer,
          [text.boundaries]: boundaryLayer,
          [text.context]: contextLayer,
        },
        { collapsed: false }
      )
      .addTo(map);

    renderAll();
    fitToItems(visibleMarkers());
    setTimeout(() => map.invalidateSize(), 0);
  };

  layerInputs.forEach((input) =>
    input.addEventListener("change", () => {
      renderAll();
      fitToItems(visibleMarkers());
    })
  );
  egknLayerInputs.forEach((input) => input.addEventListener("change", renderAll));
  if (boundaryInput) {
    boundaryInput.addEventListener("change", renderAll);
  }
  if (fitButton) {
    fitButton.addEventListener("click", () => {
      layerInputs.forEach((input) => {
        input.checked = true;
      });
      renderAll();
      fitToItems(visibleMarkers());
    });
  }

  bootLeaflet();
})();
