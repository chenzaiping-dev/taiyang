(function () {
  "use strict";

  const form = document.getElementById("stock-out-form");
  const tbody = document.querySelector("#stock-out-table tbody");
  if (!form || !tbody) return;

  const warehouseFilter = document.getElementById("warehouse-filter");
  const materialSearch = document.getElementById("material-search");
  const validationBox = document.getElementById("stock-out-validation");
  const countEl = document.getElementById("selected-line-count");
  const totalEl = document.getElementById("total-quantity");
  const submitBtn = document.getElementById("submit-btn");

  function rows() { return Array.from(tbody.querySelectorAll("tr.stock-row")); }

  function filterRows() {
    const warehouseId = warehouseFilter?.value || "";
    const keyword = (materialSearch?.value || "").trim().toLowerCase();
    rows().forEach((tr) => {
      const warehouseMatch = !warehouseId || tr.dataset.warehouseId === warehouseId;
      const searchMatch = !keyword || (tr.dataset.search || "").includes(keyword);
      tr.hidden = !(warehouseMatch && searchMatch);
    });
  }

  function validate(show) {
    const errors = [];
    let selectedCount = 0;
    let total = 0;

    rows().forEach((tr) => {
      const input = tr.querySelector(".out-quantity");
      const available = Number(tr.dataset.available || 0);
      const quantity = Number(input?.value || 0);
      const shortageEl = tr.querySelector(".row-shortage");
      if (shortageEl) shortageEl.textContent = "";
      if (input) input.setCustomValidity("");

      if (!Number.isFinite(quantity) || quantity < 0) {
        errors.push(`${tr.cells[2]?.textContent || "原料"}：出库数量不能小于 0`);
        return;
      }
      if (quantity <= 0) return;

      selectedCount += 1;
      total += quantity;
      if (quantity > available + 1e-9) {
        const shortage = quantity - available;
        const msg = `${tr.cells[2]?.textContent || "原料"} ${tr.cells[3]?.textContent || ""}：需要 ${quantity}，可用 ${available}，不足 ${shortage}`;
        errors.push(msg);
        if (shortageEl) shortageEl.textContent = `库存不足 ${shortage}`;
        if (input) input.setCustomValidity(msg);
      }
    });

    if (countEl) countEl.textContent = String(selectedCount);
    if (totalEl) totalEl.textContent = total.toFixed(6).replace(/\.?0+$/, "");

    if (show && validationBox) {
      validationBox.style.display = errors.length ? "block" : "none";
      validationBox.innerHTML = errors.length
        ? `<ul style="margin:0;padding-left:20px">${errors.map(x => `<li>${escapeHtml(x)}</li>`).join("")}</ul>`
        : "";
    }
    return { valid: errors.length === 0, selectedCount, errors };
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[ch]);
  }

  warehouseFilter?.addEventListener("change", filterRows);
  materialSearch?.addEventListener("input", filterRows);
  tbody.addEventListener("input", (event) => {
    if (event.target.matches(".out-quantity")) validate(true);
  });

  form.addEventListener("submit", (event) => {
    const result = validate(true);
    if (!result.valid) {
      event.preventDefault();
      validationBox?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (!result.selectedCount) {
      event.preventDefault();
      if (validationBox) {
        validationBox.textContent = "请至少填写一行大于 0 的出库数量";
        validationBox.style.display = "block";
      }
      return;
    }
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "提交中...";
    }
  });

  filterRows();
  validate(false);
})();
