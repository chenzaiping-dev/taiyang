/**
 * static/js/purchase_in.js
 * 采购入库表单：动态行增删、行号重排、金额合计、重复原料校验
 */
(function () {
  "use strict";

  // ---------- 元素引用 ----------
  const lineTableBody = document.querySelector("#line-table tbody");
  const lineTemplate = document.getElementById("line-template");
  const addRowBtn = document.getElementById("add-row-btn");
  const form = document.getElementById("purchase-in-form");
  const totalQtyEl = document.getElementById("total-qty");
  const totalAmountEl = document.getElementById("total-amount");

  // 起始索引：模板渲染时用 data-next-index 传进来，避免重复
  let nextIndex = parseInt(form?.dataset.nextIndex || "0", 10);

  // ---------- 工具函数 ----------
  function toNumber(v) {
    const n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }

  function formatMoney(n) {
    // 保留 6 位小数，去除多余尾零
    return n
      .toFixed(6)
      .replace(/\.?0+$/, "") || "0";
  }

  /** 重排行号（仅视觉） */
  function renumberRows() {
    lineTableBody.querySelectorAll(".line-row").forEach((tr, i) => {
      const cell = tr.querySelector(".row-no");
      if (cell) cell.textContent = i + 1;
    });
  }

  /** 重新计算所有行的合计（数量 + 金额） */
  function recalcTotals() {
    let totalQty = 0;
    let totalAmount = 0;

    lineTableBody.querySelectorAll(".line-row").forEach((tr) => {
      const qty = toNumber(tr.querySelector('input[name$="[quantity]"]')?.value);
      const cost = toNumber(tr.querySelector('input[name$="[unit_cost]"]')?.value);
      const rowTotal = qty * cost;

      // 每行金额小计（若有单元格）
      const rowTotalEl = tr.querySelector(".row-total");
      if (rowTotalEl) rowTotalEl.textContent = formatMoney(rowTotal);

      totalQty += qty;
      totalAmount += rowTotal;
    });

    if (totalQtyEl) totalQtyEl.textContent = formatMoney(totalQty);
    if (totalAmountEl) totalAmountEl.textContent = formatMoney(totalAmount);
  }

  /** 新增一行 */
  function addRow() {
    if (!lineTemplate) return;
    const html = lineTemplate.innerHTML.replaceAll("__IDX__", nextIndex++);
    lineTableBody.insertAdjacentHTML("beforeend", html);
    renumberRows();
    recalcTotals();

    // 自动聚焦新行的原料下拉
    const lastRow = lineTableBody.querySelector(".line-row:last-child");
    lastRow?.querySelector('select[name$="[material_id]"]')?.focus();
  }

  /** 删除行（至少保留一行） */
  function removeRow(btn) {
    const rows = lineTableBody.querySelectorAll(".line-row");
    if (rows.length <= 1) {
      alert("至少保留一行明细");
      return;
    }
    btn.closest("tr")?.remove();
    renumberRows();
    recalcTotals();
  }

  // ---------- 事件委托 ----------
  // 用委托，兼容后续动态插入的行
  lineTableBody.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-remove-row");
    if (btn) {
      e.preventDefault();
      removeRow(btn);
    }
  });

  // 输入时实时重算
  lineTableBody.addEventListener("input", (e) => {
    if (e.target.matches('input[name$="[quantity]"], input[name$="[unit_cost]"]')) {
      recalcTotals();
    }
  });

  // 原料选择变化时，防止同一原料重复
  lineTableBody.addEventListener("change", (e) => {
    if (!e.target.matches('select[name$="[material_id]"]')) return;
    const currentVal = e.target.value;
    if (!currentVal) return;

    let dupCount = 0;
    lineTableBody
      .querySelectorAll('select[name$="[material_id]"]')
      .forEach((sel) => {
        if (sel.value === currentVal) dupCount++;
      });

    if (dupCount > 1) {
      alert("同一原料只能出现一次，请合并数量或删除重复行");
      e.target.value = "";
      recalcTotals();
    }
  });

  // ---------- 提交前校验 ----------
  form?.addEventListener("submit", (e) => {
    const rows = lineTableBody.querySelectorAll(".line-row");
    let hasValidRow = false;
    let firstError = null;

    rows.forEach((tr, i) => {
      const material = tr.querySelector('select[name$="[material_id]"]')?.value;
      const qty = toNumber(tr.querySelector('input[name$="[quantity]"]')?.value);
      const cost = tr.querySelector('input[name$="[unit_cost]"]')?.value;

      const isEmpty =
        !material &&
        !tr.querySelector('input[name$="[quantity]"]')?.value &&
        !cost;
      if (isEmpty) return; // 全空行跳过（后端也会过滤）

      if (!material) {
        firstError = firstError || `第 ${i + 1} 行：请选择原料`;
        return;
      }
      if (!qty || qty <= 0) {
        firstError = firstError || `第 ${i + 1} 行：数量必须大于 0`;
        return;
      }
      if (cost === "" || parseFloat(cost) < 0) {
        firstError = firstError || `第 ${i + 1} 行：单价不能为空或负数`;
        return;
      }
      hasValidRow = true;
    });

    if (!hasValidRow) {
      firstError = firstError || "请至少填写一行有效明细";
    }

    if (firstError) {
      e.preventDefault();
      alert(firstError);
      return;
    }

    // 可在此禁用按钮，防止重复提交
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "提交中...";
    }
  });

  // ---------- 初始化 ----------
  addRowBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    addRow();
  });

  // 页面加载后计算一次（回填场景）
  recalcTotals();
  renumberRows();

  // 对外暴露（调试用）
  window.PurchaseInForm = { addRow, removeRow, recalcTotals };
})();