document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector(".period-form");
  const select = document.querySelector("#period-days");

  if (!form || !select) {
    return;
  }

  select.addEventListener("change", () => {
    form.submit();
  });
});
