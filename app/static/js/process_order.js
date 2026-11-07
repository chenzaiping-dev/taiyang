(function () {
  "use strict";

  const tbody    = document.querySelector("#mat-table tbody");
  const tpl      = document.getElementById("mat-template");
  const addBtn   = document.getElementById("add-row-btn");
  const form     = document.getElementById("po-form");
  const ratioSumEl = document.getElementById("ratio-sum");
  const planQtyInput = document.getElementById("planned_quantity");   // 计划数量输入框
  let nextIndex = parseInt(form?.dataset.nextIndex || "0", 10);

  const toNum = (v) => { const n = parseFloat(v); return isNaN(n) ? 0 : n; };
  const fmt   = (n) => (n.toFixed(4).replace(/\.?0+$/, "") || "0");

  function renumber() {
    tbody.querySelectorAll(".mat-row").forEach((tr, i) => {
      const c = tr.querySelector(".row-no");
      if (c) c.textContent = i + 1;
    });
  }

  function recalcRatio() {
    let sum = 0;
    tbody.querySelectorAll('input[name$="[ratio_value]"]').forEach((inp) => {
      sum += toNum(inp.value);
    });
    ratioSumEl.textContent = fmt(sum);
    ratioSumEl.style.color = Math.abs(sum - 1) < 1e-6 ? "#2e7d32" : "#c62828";
    return sum;
  }

  // ⭐ 按计划数量 × 配比，重算每行的数量
  function recalcQuantities() {
    const planQty = toNum(planQtyInput?.value);
    tbody.querySelectorAll(".mat-row").forEach((tr) => {
      const ratioInput = tr.querySelector('input[name$="[ratio_value]"]');
      const qtyInput   = tr.querySelector('input[name$="[quantity]"]');
      if (!qtyInput) return;
      const ratio = toNum(ratioInput?.value);
      qtyInput.value = planQty > 0 && ratio > 0 ? fmt(planQty * ratio) : "";
    });
  }

  function addRow() {
    tbody.insertAdjacentHTML(
      "beforeend",
      tpl.innerHTML.replaceAll("__IDX__", nextIndex++)
    );
    renumber();
    recalcRatio();
    recalcQuantities();
  }

  function removeRow(btn) {
    if (tbody.querySelectorAll(".mat-row").length <= 1) {
      alert("至少保留一行原料");
      return;
    }
    btn.closest("tr")?.remove();
    renumber();
    recalcRatio();
    recalcQuantities();
  }

  // ---------- 事件绑定 ----------
  tbody.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-remove-row");
    if (btn) { e.preventDefault(); removeRow(btn); }
  });

  tbody.addEventListener("input", (e) => {
    if (e.target.matches('input[name$="[ratio_value]"]')) {
      recalcRatio();
      recalcQuantities();     // ⭐ 配比变化 → 重算数量
    }
  });

  // ⭐ 计划数量变化 → 重算所有行数量
  planQtyInput?.addEventListener("input", () => {
    recalcQuantities();
  });

  tbody.addEventListener("change", (e) => {
    if (!e.target.matches('select[name$="[material_id]"]')) return;
    const v = e.target.value;
    if (!v) return;
    let cnt = 0;
    tbody.querySelectorAll('select[name$="[material_id]"]').forEach((s) => {
      if (s.value === v) cnt++;
    });
    if (cnt > 1) {
      alert("同一原料只能出现一次");
      e.target.value = "";
    }
  });

  form?.addEventListener("submit", (e) => {
    const planQty = toNum(planQtyInput?.value);
    let sum = 0, err = null, valid = false;

    tbody.querySelectorAll(".mat-row").forEach((tr, i) => {
      const mid = tr.querySelector('select[name$="[material_id]"]')?.value;
      const ratioInput = tr.querySelector('input[name$="[ratio_value]"]');
      const ratio = toNum(ratioInput?.value);
      const isEmpty = !mid && !ratioInput?.value;
      if (isEmpty) return;
      if (!mid) { err = err || `第 ${i + 1} 行：请选择原料`; return; }
      if (ratioInput.value === "" || ratio < 0 || ratio > 1) {
        err = err || `第 ${i + 1} 行：配比必须在 0~1 之间`; return;
      }
      sum += ratio;
      valid = true;
    });

    if (!valid) err = err || "请至少填写一条原料配比";
    if (!err && planQty <= 0) err = "计划数量必须大于 0";
    if (!err && sum > 1 + 1e-6) {
      err = `原料配比合计必须小于等于 1，当前为 ${sum.toFixed(6)}`;
    }
    if (err) { e.preventDefault(); alert(err); return; }

    // 提交前再做一次最终重算，保证提交值与页面一致
    recalcQuantities();

    const btn = form.querySelector('button[type="submit"]');
    if (btn) { btn.disabled = true; btn.textContent = "保存中..."; }
  });

  // ============================================================
  // ⭐ 从其他工艺单复制配比（AJAX 版）
  // ============================================================
  const copyFromBtn = document.getElementById("copy-from-btn");

  async function loadCopyFrom(orderId) {
    if (!orderId) {
      alert("请先搜索并选择一个工艺单");
      return;
    }
    if (!confirm("载入后会覆盖当前原料配比，是否继续？")) return;

    let data;
    try {
      const resp = await fetch(`/process-orders/api/${orderId}/materials`, {
        credentials: "same-origin",
      });
      data = await resp.json();
    } catch (e) {
      alert("获取配比失败: " + e);
      return;
    }
    if (!data || data.code !== 0) {
      alert("获取配比失败: " + (data?.msg || "未知错误"));
      return;
    }
    const items = data.data?.materials || [];
    if (!items.length) {
      alert("该工艺单没有原料配比");
      return;
    }

    // 清空 tbody
    tbody.innerHTML = "";

    // 用模板逐行插入
    items.forEach((it) => {
      const html = tpl.innerHTML.replaceAll("__IDX__", nextIndex++);
      tbody.insertAdjacentHTML("beforeend", html);
      const tr = tbody.querySelector(".mat-row:last-child");

      const sel = tr.querySelector('select[name$="[material_id]"]');
      if (sel) sel.value = String(it.material_id);

      const ratioInput = tr.querySelector('input[name$="[ratio_value]"]');
      if (ratioInput) ratioInput.value = it.ratio_value;

      const qtyInput = tr.querySelector('input[name$="[quantity]"]');
      if (qtyInput) {
        const planQty = toNum(planQtyInput?.value);
        qtyInput.value = planQty > 0 ? "" : (it.quantity || "");
      }

      const remarkInput = tr.querySelector('input[name$="[remark]"]');
      if (remarkInput) remarkInput.value = it.remark || "";
    });

    renumber();
    recalcRatio();
    recalcQuantities();
  }

  copyFromBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    // 从 hidden input 取当前选中的工艺单 ID（由 po_suggest.js 写入）
    const hidden = document.querySelector(
      '#po-form [name="__copy_from_process_order_id"]'
    );
    loadCopyFrom(hidden?.value || "");
  });

  addBtn?.addEventListener("click", (e) => { e.preventDefault(); addRow(); });
  renumber();
  recalcRatio();
  recalcQuantities();
})();
