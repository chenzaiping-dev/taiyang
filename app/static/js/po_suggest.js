/**
 * static/js/po_suggest.js
 * 自定义工艺单搜索下拉
 *
 * 每个 .po-suggest 需带属性：
 *   data-input-name  提交字段名，如 lines[0][process_order_id]
 *   data-initial-id  初始选中的工艺单 id（可空）
 *   data-initial-no  初始选中的工艺单号文本（可空）
 */
(function () {
  "use strict";

  const SEARCH_URL = "/finished-goods/api/process-orders/search";
  const DEBOUNCE_MS = 250;

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  class POSuggest {
    constructor(root) {
      this.root = root;
      this.inputName = root.dataset.inputName || "";
      this.initialId = root.dataset.initialId || "";
      this.initialNo = root.dataset.initialNo || "";

      this.selectedId = "";
      this.selectedNo = "";
      this.activeIndex = -1;
      this.candidates = [];
      this._timer = null;
      this._reqSeq = 0;
      this._boundDocClick = null;

      this._build();
      this._bind();
      this._initFromInitial();
    }

    _build() {
      this.hidden = document.createElement("input");
      this.hidden.type = "hidden";
      this.hidden.name = this.inputName;
      this.hidden.value = "";
      this.root.appendChild(this.hidden);

      this.input = document.createElement("input");
      this.input.type = "text";
      this.input.className = "po-suggest-input";
      this.input.autocomplete = "off";
      this.input.placeholder = "输入工艺单号（可留空）";
      this.root.appendChild(this.input);

      this.clearBtn = document.createElement("span");
      this.clearBtn.className = "po-suggest-clear";
      this.clearBtn.textContent = "×";
      this.clearBtn.title = "清除";
      this.root.appendChild(this.clearBtn);

      this.dropdown = document.createElement("ul");
      this.dropdown.className = "po-suggest-dropdown";
      this.root.appendChild(this.dropdown);
    }

    _bind() {
      this.input.addEventListener("input", () => this._onInput());
      this.input.addEventListener("focus", () => this._openIfHasCandidates());
      this.input.addEventListener("blur", () => setTimeout(() => this._close(), 150));
      this.input.addEventListener("keydown", (e) => this._onKeyDown(e));

      this.clearBtn.addEventListener("mousedown", (e) => {
        e.preventDefault();
        this._setSelected("", "");
        this.input.focus();
      });

      this.dropdown.addEventListener("mousedown", (e) => {
        const li = e.target.closest("li[data-id]");
        if (!li) return;
        e.preventDefault();
        this._setSelected(li.dataset.id, li.dataset.no);
        this._close();
      });

      this._boundDocClick = (e) => {
        if (!this.root.contains(e.target)) this._close();
      };
      document.addEventListener("mousedown", this._boundDocClick);
    }

    _initFromInitial() {
      if (this.initialId) {
        if (this.initialNo) {
          this._setSelected(this.initialId, this.initialNo);
        } else {
          this._fetchById(this.initialId).then((item) => {
            if (item) this._setSelected(item.id, item.process_order_no);
          });
        }
      }
    }

    _setSelected(id, no) {
      this.selectedId = String(id || "");
      this.selectedNo = String(no || "");
      this.hidden.value = this.selectedId;
      this.input.value = this.selectedNo;
      this.root.classList.toggle("has-value", !!this.selectedId);
    }

    _onInput() {
      const kw = this.input.value.trim();
      if (kw !== this.selectedNo) {
        this.hidden.value = "";
        this.selectedId = "";
        this.root.classList.remove("has-value");
      }
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this._search(kw), DEBOUNCE_MS);
    }

    _onKeyDown(e) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!this.dropdown.classList.contains("show")) {
          this._search(this.input.value.trim());
          return;
        }
        this._moveActive(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        this._moveActive(-1);
      } else if (e.key === "Enter") {
        if (this.dropdown.classList.contains("show") && this.activeIndex >= 0) {
          e.preventDefault();
          const item = this.candidates[this.activeIndex];
          if (item) {
            this._setSelected(item.id, item.process_order_no);
            this._close();
          }
        }
      } else if (e.key === "Escape") {
        this._close();
      }
    }

    _moveActive(delta) {
      if (!this.candidates.length) return;
      const len = this.candidates.length;
      this.activeIndex = (this.activeIndex + delta + len) % len;
      this._render(true);
      const activeLi = this.dropdown.querySelectorAll("li[data-id]")[this.activeIndex];
      if (activeLi) activeLi.scrollIntoView({ block: "nearest" });
    }

    _openIfHasCandidates() {
      if (this.candidates.length) {
        this.dropdown.classList.add("show");
        this._render();
      } else {
        this._search("");
      }
    }

    _close() {
      this.dropdown.classList.remove("show");
      this.activeIndex = -1;
    }

    _search(keyword) {
      const mySeq = ++this._reqSeq;
      fetch(`${SEARCH_URL}?q=${encodeURIComponent(keyword)}`,
            { credentials: "same-origin" })
        .then((r) => r.json())
        .then((body) => {
          if (mySeq !== this._reqSeq) return;
          this.candidates = (body && body.data) || [];
          this.activeIndex = this.candidates.length ? 0 : -1;
          this._render();
          this.dropdown.classList.add("show");
        })
        .catch(() => {
          if (mySeq !== this._reqSeq) return;
          this.candidates = [];
          this._render();
          this.dropdown.classList.add("show");
        });
    }

    _fetchById(id) {
      return fetch(`${SEARCH_URL}?id=${encodeURIComponent(id)}`,
                   { credentials: "same-origin" })
        .then((r) => r.json())
        .then((body) => (body.data && body.data[0]) || null)
        .catch(() => null);
    }

    _render(highlightActive) {
      if (!this.candidates.length) {
        this.dropdown.innerHTML =
          '<div class="po-suggest-empty">无匹配工艺单</div>';
        return;
      }
      this.dropdown.innerHTML = this.candidates.map((it, idx) => {
        const active = highlightActive && idx === this.activeIndex ? "active" : "";
        return `
          <li data-id="${escapeHtml(it.id)}" data-no="${escapeHtml(it.process_order_no)}" class="${active}">
            <div>
              <span class="po-no">${escapeHtml(it.process_order_no)}</span>
              — ${escapeHtml(it.product_name || "")}
            </div>
            <div class="po-meta">
              批次 ${escapeHtml(it.batch_no || "-")} · 数量 ${escapeHtml(it.quantity || "-")} ${escapeHtml(it.unit || "")}
            </div>
          </li>`;
      }).join("");
    }

    destroy() {
      document.removeEventListener("mousedown", this._boundDocClick);
      this.root.innerHTML = "";
      delete this.root.__pos_ready__;
    }
  }

  window.POSuggest = {
    initAll() {
      document.querySelectorAll(".po-suggest").forEach((node) => {
        if (node.__pos_ready__) return;
        node.__pos_ready__ = true;
        new POSuggest(node);
      });
    },
  };
})();