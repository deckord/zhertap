const planRegion = document.querySelector("#plan-region-select");
const planDistrict = document.querySelector("#plan-district-select");
const planLocality = document.querySelector("#plan-locality-select");
const planSubmit = document.querySelector("#plan-submit");
const planStatus = document.querySelector("#plan-catalog-status");

function planSetStatus(message, isError = false) {
  planStatus.textContent = message;
  planStatus.classList.toggle("error", isError);
}

function planSetOptions(select, rows, placeholder, allowEmpty = false) {
  select.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = placeholder;
  empty.selected = true;
  empty.disabled = !allowEmpty;
  select.append(empty);
  for (const row of rows) {
    const option = document.createElement("option");
    option.value = row.value;
    option.textContent = row.label;
    if (row.id !== undefined) option.dataset.id = String(row.id);
    select.append(option);
  }
}

async function planGet(url) {
  const response = await fetch(url, {credentials: "same-origin", headers: {Accept: "application/json"}});
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "ЕГКН не ответил");
  }
  return response.json();
}

async function loadPlanRegions() {
  try {
    const rows = await planGet("/admin/catalog/regions");
    planSetOptions(planRegion, rows, "Выберите область");
    planRegion.disabled = false;
    planSetStatus("Выберите территорию документа.");
  } catch (error) {
    planSetStatus(`Не удалось загрузить области: ${error.message}`, true);
  }
}

async function loadPlanDistricts() {
  planDistrict.disabled = true;
  planLocality.disabled = true;
  planSubmit.disabled = true;
  planSetOptions(planDistrict, [], "Загрузка районов...");
  try {
    const rows = await planGet(`/admin/catalog/districts?region=${encodeURIComponent(planRegion.value)}`);
    planSetOptions(planDistrict, rows, "Выберите район");
    planDistrict.disabled = false;
  } catch (error) {
    planSetStatus(`Не удалось загрузить районы: ${error.message}`, true);
  }
}

async function loadPlanLocalities() {
  const option = planDistrict.options[planDistrict.selectedIndex];
  const districtId = option?.dataset.id;
  planSubmit.disabled = !districtId;
  if (!districtId) return;
  planSetOptions(planLocality, [], "Загрузка населенных пунктов...", true);
  try {
    const rows = await planGet(`/admin/catalog/settlements?district_id=${encodeURIComponent(districtId)}`);
    planSetOptions(planLocality, rows, "Весь район", true);
    planLocality.disabled = false;
    planSetStatus("Выберите населенный пункт или оставьте «Весь район».");
  } catch (error) {
    planSetOptions(planLocality, [], "Весь район", true);
    planLocality.disabled = false;
    planSetStatus(`Населенные пункты недоступны: ${error.message}. Можно загрузить слой всего района.`, true);
  }
}

planRegion.addEventListener("change", loadPlanDistricts);
planDistrict.addEventListener("change", loadPlanLocalities);
loadPlanRegions();
