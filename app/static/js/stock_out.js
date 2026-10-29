(function () {
  "use strict";

  const tbody = document.querySelector("#line-table tbody");
  const tpl = document.getElementById("line-template");
  const addBtn = document.getElementById("add-row-btn");
  const form = document.getElementById("stock-out-form");
  let nextIndex = parseInt(form?.dataset.nextIndex || "0", 10);

  function renumber() {
    tbody.querySelectorAll(".line-row").forEach((tr, i) => {
      const c = tr.querySelector(".row-no");
      if (c) c.textContent = i + 1;
    });
  }

  function addRow() {
    tbody.insertAdjacentHTML(
      "beforeend",
      tpl.innerHTML.replaceAll("__IDX__", nextIndex++)
    );
    renumber();
    tbody.querySelector(".line-row:last-child select")?.focus();
  }

  function removeRow(btn) {
    if (tbody.querySelectorAll(".line-row").length <= 1) {
      alert("至少保留一行明细");
      return;
    }
    btn.closest("tr")?.remove();
    renumber();
  }

  tbody.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-remove-row");
    if (btn) { e.preventDefault(); removeRow(btn); }
  });

  tbody.addEventListener("change", (e) => {
    if (!e.target.matches('select[name$="[material_id]"]')) return;
    const val = e.target.value;
    if (!val) return;
    let cnt = 0;
    tbody.querySelectorAll('select[name$="[material_id]"]')
      .forEach((s) => { if (s.value === val) cnt++; });
    if (cnt > 1) {
      alert("同一原料只能出现一次");
      e.target.value = "";
    }
  });

  form?.addEventListener("submit", (e) => {
    let valid = false, err = null;
    tbody.querySelectorAll(".line-row").forEach((tr, i) => {
      const m = tr.querySelector('select[name$="[material_id]"]')?.value;
      const q = parseFloat(tr.querySelector('input[name$="[quantity]"]')?.value);
      if (!m && !q) return;
      if (!m) { err = err || `第 ${i + 1} 行：请选择原料`; return; }
      if (!q || q <= 0) { err = err || `第 ${i + 1} 行：数量必须 > 0`; return; }
      valid = true;
    });
    if (err || !valid) {
      e.preventDefault();
      alert(err || "请至少填写一行有效明细");
      return;
    }
    const btn = form.querySelector('button[type="submit"]');
    if (btn) { btn.disabled = true; btn.textContent = "提交中..."; }
  });

  addBtn?.addEventListener("click", (e) => { e.preventDefault(); addRow(); });
  renumber();
})();