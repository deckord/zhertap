(function () {
  function option(value, label, extra = {}) {
    const item = document.createElement("option");
    item.value = value;
    item.textContent = label;
    for (const [key, fieldValue] of Object.entries(extra)) {
      item.dataset[key] = String(fieldValue);
    }
    return item;
  }

  function setOptions(select, rows, placeholder, selectedValue = "", allowEmpty = true) {
    select.replaceChildren();
    if (allowEmpty) {
      select.append(option("", placeholder));
    }
    for (const row of rows) {
      select.append(option(row.value, row.label, row.id === undefined ? {} : {id: row.id}));
    }
    select.value = selectedValue && [...select.options].some((item) => item.value === selectedValue)
      ? selectedValue
      : "";
  }

  async function getJson(path, params = {}) {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, value);
      }
    }
    const response = await fetch(url.href, {
      credentials: "same-origin",
      headers: {Accept: "application/json"},
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "Справочник временно недоступен");
    }
    return response.json();
  }

  function setStatus(node, message, isError = false) {
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("is-error", isError);
  }

  function initSearchCatalog() {
    const form = document.querySelector('[data-catalog-form="search"]');
    if (!form) return;
    const catalogBase = form.getAttribute("action") === "/guest-search" ? "/catalog" : "/cabinet/catalog";
    const region = document.querySelector("#search-region");
    const district = document.querySelector("#search-district");
    const locality = document.querySelector("#search-locality");
    const submit = document.querySelector("#search-submit");
    const status = document.querySelector("#search-catalog-status");
    const retry = document.querySelector('[data-catalog-retry="search"]');
    const purpose = document.querySelector("#search-purpose");
    const irrigation = document.querySelector("#search-irrigation");
    const irrigationField = document.querySelector("#search-irrigation-field");
    const gardeningArea = document.querySelector("#search-gardening-area");
    const gardeningAreaField = document.querySelector("#search-gardening-area-field");
    const fixedAreaNote = document.querySelector("#search-fixed-area-note");

    function updateSubmit() {
      submit.disabled = !(region.value && district.value);
    }

    function updatePurposeArea() {
      if (
        !purpose ||
        !irrigation ||
        !irrigationField ||
        !gardeningArea ||
        !gardeningAreaField ||
        !fixedAreaNote
      ) return;
      const isNewLph = purpose.value === "ЛПХ(новый поиск)";
      const isGardening = purpose.value === "Садоводство";
      irrigation.disabled = !isNewLph;
      irrigationField.classList.toggle("is-hidden", !isNewLph);
      gardeningArea.disabled = !isGardening;
      gardeningAreaField.classList.toggle("is-hidden", !isGardening);
      fixedAreaNote.classList.toggle("is-hidden", isNewLph || isGardening);
      if (isNewLph) {
        fixedAreaNote.textContent = "";
      } else {
        fixedAreaNote.textContent = "Базовый ЛПХ анализируется как 10 соток (0.10 га).";
      }
    }

    async function loadRegions() {
      if (retry) retry.hidden = true;
      try {
        const rows = await getJson(`${catalogBase}/regions`);
        setOptions(region, rows, "Выберите область", "", true);
        region.disabled = false;
        setStatus(status, "Выберите область, затем район и населенный пункт.");
      } catch (error) {
        setOptions(region, [], "Справочник недоступен", "", true);
        region.disabled = true;
        submit.disabled = true;
        if (retry) retry.hidden = false;
        setStatus(status, error.message, true);
      }
    }

    async function loadDistricts() {
      district.disabled = true;
      locality.disabled = true;
      submit.disabled = true;
      setOptions(district, [], "Загрузка районов...", "", true);
      setOptions(locality, [], "Сначала выберите район", "", true);
      if (!region.value) return;
      try {
        const rows = await getJson(`${catalogBase}/districts`, {region: region.value});
        setOptions(district, rows, "Выберите район", "", true);
        district.disabled = false;
        setStatus(status, "Теперь выберите район.");
      } catch (error) {
        setOptions(district, [], "Справочник недоступен", "", true);
        setStatus(status, error.message, true);
      }
    }

    async function loadLocalities() {
      locality.disabled = true;
      submit.disabled = true;
      const selected = district.options[district.selectedIndex];
      const districtId = selected?.dataset.id;
      setOptions(locality, [], "Загрузка населенных пунктов...", "", true);
      if (!districtId) return;
      try {
        const rows = await getJson(`${catalogBase}/settlements`, {district_id: districtId});
        setOptions(locality, rows, "Искать по всему району", "", true);
        locality.disabled = false;
        setStatus(status, rows.length ? "Можно выбрать населенный пункт или искать по всему району." : "По району можно запускать поиск без населенного пункта.");
      } catch (error) {
        setOptions(locality, [], "Справочник недоступен", "", true);
        setStatus(status, error.message, true);
      } finally {
        updateSubmit();
      }
    }

    region.addEventListener("change", loadDistricts);
    district.addEventListener("change", loadLocalities);
    locality.addEventListener("change", updateSubmit);
    purpose?.addEventListener("change", updatePurposeArea);
    form.addEventListener("submit", (event) => {
      const regionValue = region.value.trim();
      const districtValue = district.value.trim();
      if (!regionValue || !districtValue) {
        event.preventDefault();
        setStatus(status, "Выберите область и район перед запуском анализа.", true);
        (regionValue ? district : region).focus();
        updateSubmit();
        return;
      }
      // Select values are omitted from FormData while disabled during a catalog refresh.
      region.disabled = false;
      district.disabled = false;
    });
    if (retry) retry.addEventListener("click", loadRegions);
    updatePurposeArea();
    loadRegions();
  }

  function initAuctionCatalog() {
    const form = document.querySelector('[data-catalog-form="auctions"]');
    if (!form) return;
    const region = document.querySelector("#auction-region");
    const district = document.querySelector("#auction-district");
    const locality = document.querySelector("#auction-locality");
    const purpose = document.querySelector("#auction-purpose");
    const lotScope = form.querySelector('[name="lot_scope"]');
    const status = document.querySelector("#auction-catalog-status");
    const retry = document.querySelector('[data-catalog-retry="auctions"]');
    const watchlistForm = document.querySelector(".auction-v2-watchlist-form");
    const flowSteps = {
      region: document.querySelector('[data-auction-catalog-step="region"]'),
      district: document.querySelector('[data-auction-catalog-step="district"]'),
      locality: document.querySelector('[data-auction-catalog-step="locality"]'),
    };
    const initial = {
      region: region.dataset.selected || "",
      district: district.dataset.selected || "",
      locality: locality.dataset.selected || "",
      purpose: purpose.dataset.selected || "",
    };

    function setFlowStep(name, state, title, detail) {
      const node = flowSteps[name];
      if (!node) return;
      node.classList.toggle("is-ready", state === "ready");
      node.classList.toggle("is-active", state === "active");
      node.classList.toggle("is-loading", state === "loading");
      node.classList.toggle("is-blocked", state === "blocked");
      const strong = node.querySelector("strong");
      const small = node.querySelector("small");
      if (strong) strong.textContent = title;
      if (small) small.textContent = detail;
    }

    function syncPurposeFilter() {
      const target = form.querySelector('input[type="hidden"][name="purpose"]');
      if (!target || !purpose) return;
      target.value = [...purpose.selectedOptions].map((item) => item.value).filter(Boolean).join(",");
    }

    function syncWatchlistFilter() {
      if (!watchlistForm) return;
      const fieldNames = [
        "lot_scope",
        "region",
        "district",
        "locality",
        "purpose",
        "min_price_kzt",
        "max_price_kzt",
        "min_area_ha",
        "max_area_ha",
        "min_score",
        "eqazyna_status",
        "risk_level",
        "confidence_level",
        "stage",
        "deadline_status",
        "geo_status",
      ];
      for (const name of fieldNames) {
        const source = form.querySelector(`[name="${name}"]`);
        const target = watchlistForm.querySelector(`input[type="hidden"][name="${name}"]`);
        if (source && target) {
          target.value = source.value;
        }
      }
    }

    function auctionCatalogParams(extra = {}) {
      return {
        ...extra,
        lot_scope: lotScope?.value || "",
      };
    }

    async function loadPurposes({updateStatus = false} = {}) {
      const rows = await getJson("/cabinet/auctions/catalog/purposes", auctionCatalogParams({
        region: region.value,
        district: district.value,
        locality: locality.value,
      }));
      const selectedValues = (purpose.dataset.selected || form.querySelector('input[name="purpose"]')?.value || "")
        .split(",").map((value) => value.trim()).filter(Boolean);
      purpose.replaceChildren();
      for (const row of rows) {
        const item = option(row.value, row.label, row.id === undefined ? {} : {id: row.id});
        item.selected = selectedValues.includes(row.value);
        purpose.append(item);
      }
      purpose.dataset.selected = "";
      syncPurposeFilter();
      if (updateStatus) {
        setStatus(status, "Назначения обновлены под выбранную географию. Можно уточнить фильтр или сразу показать лоты.");
      }
      syncWatchlistFilter();
    }

    async function loadRegions() {
      if (retry) retry.hidden = true;
      const selectedRegion = region.dataset.selected || region.value || initial.region;
      const selectedDistrict = district.dataset.selected || district.value || initial.district;
      const selectedLocality = locality.dataset.selected || locality.value || initial.locality;
      setStatus(status, "Справочники аукционов загружаются из ЕГКН и базы E-Qazyna.");
      setFlowStep("region", "loading", "1. Регион", "Загружаем список регионов");
      setFlowStep("district", "blocked", "2. Район", "Сначала выберите регион");
      setFlowStep("locality", "blocked", "3. Населенный пункт", "Сначала выберите район");
      const rows = await getJson("/cabinet/auctions/catalog/regions", auctionCatalogParams());
      setOptions(region, rows, "Все регионы", selectedRegion, true);
      setFlowStep(
        "region",
        region.value ? "ready" : "active",
        "1. Регион",
        region.value ? `Выбрано: ${region.value}` : `Загружено: ${rows.length}. Выберите регион`
      );
      await loadDistricts(selectedDistrict, selectedLocality);
      await loadPurposes();
      if (!region.value) {
        setStatus(
          status,
          `Загружено регионов: ${rows.length}. Если оставить "Все регионы", поиск покажет лоты по всему Казахстану; район и населенный пункт появятся после выбора региона.`
        );
      }
      region.dataset.selected = "";
      syncWatchlistFilter();
    }

    async function loadDistricts(selectedDistrict = "", selectedLocality = "") {
      district.disabled = true;
      locality.disabled = true;
      setOptions(district, [], region.value ? "Загрузка районов..." : "Сначала выберите регион", "", true);
      setOptions(locality, [], "Сначала выберите район", "", true);
      setFlowStep(
        "region",
        region.value ? "ready" : "active",
        "1. Регион",
        region.value ? `Выбрано: ${region.value}` : "Выберите регион, чтобы открыть районы"
      );
      setFlowStep(
        "district",
        region.value ? "loading" : "blocked",
        "2. Район",
        region.value ? "Загружаем районы" : "Сначала выберите регион"
      );
      setFlowStep("locality", "blocked", "3. Населенный пункт", "Сначала выберите район");
      if (!region.value) {
        district.disabled = true;
        locality.disabled = true;
        await loadPurposes();
        setStatus(
          status,
          "Сейчас выбран поиск по всем регионам. Чтобы увидеть районы и населенные пункты, сначала выберите конкретный регион."
        );
        syncWatchlistFilter();
        return;
      }
      const rows = await getJson("/cabinet/auctions/catalog/districts", auctionCatalogParams({region: region.value}));
      setOptions(district, rows, "Все районы", selectedDistrict, true);
      district.disabled = false;
      setFlowStep(
        "district",
        district.value ? "ready" : "active",
        "2. Район",
        district.value ? `Выбрано: ${district.value}` : `Загружено: ${rows.length}. Можно выбрать район`
      );
      await loadLocalities(selectedLocality);
      await loadPurposes();
      setStatus(
        status,
        `Загружено районов: ${rows.length}. Можно оставить "Все районы" или выбрать район, чтобы открыть населенные пункты.`
      );
      syncWatchlistFilter();
    }

    async function loadLocalities(selectedLocality = "") {
      locality.disabled = true;
      setOptions(locality, [], district.value ? "Загрузка населенных пунктов..." : "Все населенные пункты", "", true);
      setFlowStep(
        "district",
        district.value ? "ready" : region.value ? "active" : "blocked",
        "2. Район",
        district.value ? `Выбрано: ${district.value}` : region.value ? "Можно искать по всему региону или выбрать район" : "Сначала выберите регион"
      );
      setFlowStep(
        "locality",
        district.value ? "loading" : "blocked",
        "3. Населенный пункт",
        district.value ? "Загружаем населенные пункты" : "Сначала выберите район"
      );
      const selectedDistrict = district.options[district.selectedIndex];
      const rows = await getJson("/cabinet/auctions/catalog/localities", auctionCatalogParams({
        region: region.value,
        district: district.value,
        district_id: selectedDistrict?.dataset.id,
      }));
      setOptions(locality, rows, "Все населенные пункты", selectedLocality, true);
      locality.disabled = !(region.value || district.value);
      setFlowStep(
        "locality",
        district.value ? (locality.value ? "ready" : "active") : "blocked",
        "3. Населенный пункт",
        district.value
          ? locality.value
            ? `Выбрано: ${locality.value}`
            : `Загружено: ${rows.length}. Можно выбрать населенный пункт`
          : "Сначала выберите район"
      );
      if (district.value) {
        setStatus(
          status,
          `Загружено населенных пунктов: ${rows.length}. Можно оставить "Все населенные пункты" для поиска по району.`
        );
      } else if (region.value) {
        setStatus(
          status,
          "Район не выбран: поиск идет по всему региону. Населенные пункты появятся после выбора района."
        );
      }
      syncWatchlistFilter();
    }

    region.addEventListener("change", async () => {
      await loadDistricts();
      syncWatchlistFilter();
    });
    district.addEventListener("change", async () => {
      await loadLocalities();
      await loadPurposes();
      syncWatchlistFilter();
    });
    locality.addEventListener("change", async () => {
      await loadPurposes({updateStatus: true});
      syncWatchlistFilter();
    });
    lotScope?.addEventListener("change", async () => {
      await loadRegions();
      syncWatchlistFilter();
    });
    purpose?.addEventListener("change", syncPurposeFilter);
    form.addEventListener("input", syncWatchlistFilter);
    form.addEventListener("change", syncWatchlistFilter);
    watchlistForm?.addEventListener("submit", syncWatchlistFilter);
    loadRegions().catch(() => {
      setOptions(region, [], "Справочник недоступен", "", true);
      region.disabled = true;
      setStatus(status, "Справочник временно недоступен. Попробуйте повторить загрузку.", true);
      if (retry) retry.hidden = false;
    });
    if (retry) retry.addEventListener("click", () => {
      loadRegions().catch(() => {
        setOptions(region, [], "Справочник недоступен", "", true);
        region.disabled = true;
        setStatus(status, "Справочник временно недоступен. Попробуйте повторить загрузку.", true);
        retry.hidden = false;
      });
    });
  }

  initSearchCatalog();
  initAuctionCatalog();
})();
