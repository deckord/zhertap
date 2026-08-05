const regionSelect = document.querySelector("#region-select");
const districtSelect = document.querySelector("#district-select");
const localitySelect = document.querySelector("#locality-select");
const submitButton = document.querySelector("#search-submit");
const catalogStatus = document.querySelector("#catalog-status");
const purposeSelect = document.querySelector("#purpose-select");
const areaDisplay = document.querySelector("#area-display");
const allotmentField = document.querySelector("#allotment-field");
const irrigationField = document.querySelector("#irrigation-field");
const irrigationSelect = document.querySelector("#irrigation-select");

function setStatus(message, isError = false) {
  catalogStatus.textContent = message;
  catalogStatus.classList.toggle("error", isError);
}

function setOptions(select, rows, placeholder) {
  select.replaceChildren();
  const emptyOption = document.createElement("option");
  emptyOption.value = "";
  emptyOption.textContent = placeholder;
  emptyOption.selected = true;
  emptyOption.disabled = true;
  select.append(emptyOption);
  for (const row of rows) {
    const option = document.createElement("option");
    option.value = row.value;
    option.textContent = row.label;
    if (row.id !== undefined) option.dataset.id = String(row.id);
    select.append(option);
  }
}

async function getCatalog(url) {
  const safeUrl = new URL(url, window.location.origin);
  const response = await fetch(safeUrl.href, {
    credentials: "same-origin",
    headers: {Accept: "application/json"},
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "ЕГКН не ответил");
  }
  return response.json();
}

async function loadRegions() {
  try {
    const rows = await getCatalog("/admin/catalog/regions");
    const preferred = "Акмолинская область";
    setOptions(regionSelect, rows, "Выберите область");
    regionSelect.disabled = false;
    regionSelect.value = rows.some((row) => row.value === preferred) ? preferred : "";
    if (regionSelect.value) await loadDistricts();
  } catch (error) {
    setStatus(`Не удалось загрузить области: ${error.message}`, true);
  }
}

async function loadDistricts() {
  districtSelect.disabled = true;
  localitySelect.disabled = true;
  submitButton.disabled = true;
  setOptions(districtSelect, [], "Загрузка районов...");
  setOptions(localitySelect, [], "Сначала выберите район");
  setStatus("Загружаю районы из ЕГКН...");
  try {
    const rows = await getCatalog(
      `/admin/catalog/districts?region=${encodeURIComponent(regionSelect.value)}`,
    );
    setOptions(districtSelect, rows, "Выберите район");
    districtSelect.disabled = false;
    setStatus("Выберите район.");
  } catch (error) {
    setOptions(districtSelect, [], "Справочник недоступен");
    setStatus(`Не удалось загрузить районы: ${error.message}`, true);
  }
}

async function loadLocalities() {
  localitySelect.disabled = true;
  submitButton.disabled = true;
  const option = districtSelect.options[districtSelect.selectedIndex];
  const districtId = option?.dataset.id;
  if (!districtId) return;
  setOptions(localitySelect, [], "Загрузка населенных пунктов...");
  setStatus("Загружаю населенные пункты из ЕГКН...");
  try {
    const rows = await getCatalog(
      `/admin/catalog/settlements?district_id=${encodeURIComponent(districtId)}`,
    );
    setOptions(localitySelect, rows, "Выберите населенный пункт");
    localitySelect.disabled = false;
    setStatus(`Доступно населенных пунктов: ${rows.length}.`);
  } catch (error) {
    setOptions(localitySelect, [], "Справочник недоступен");
    setStatus(`Не удалось загрузить населенные пункты: ${error.message}`, true);
  }
}

regionSelect.addEventListener("change", loadDistricts);
districtSelect.addEventListener("change", loadLocalities);
localitySelect.addEventListener("change", () => {
  submitButton.disabled = !localitySelect.value;
});
function updatePurposeFields() {
  const isNewLph = purposeSelect?.value === "ЛПХ(новый поиск)";
  allotmentField.hidden = !isNewLph;
  irrigationField.hidden = !isNewLph;
  if (purposeSelect?.value === "Садоводство") {
    areaDisplay.value = "12 соток (0.12 га)";
  } else if (isNewLph) {
    areaDisplay.value = irrigationSelect.value === "irrigated"
      ? "15 соток (0.15 га)"
      : "25 соток (0.25 га)";
  } else {
    areaDisplay.value = "10 соток (0.10 га)";
  }
}

purposeSelect?.addEventListener("change", updatePurposeFields);
irrigationSelect?.addEventListener("change", updatePurposeFields);
updatePurposeFields();

loadRegions();
