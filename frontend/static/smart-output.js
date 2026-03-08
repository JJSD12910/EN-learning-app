(function initSmartOutput(global) {
  "use strict";

  const KEY_LABELS = {
    title: "Title",
    name: "Name",
    status: "Status",
    effective_status: "Runtime",
    type: "Type",
    exam_id: "Exam ID",
    attempt_id: "Attempt ID",
    practice_exam_id: "Practice ID",
    class_id: "Class ID",
    class_name: "Class",
    student_id: "Student ID",
    student_no: "Student No",
    client_username: "Student Account",
    teacher_username: "Teacher",
    score: "Score",
    total: "Total",
    accuracy: "Accuracy",
    question_count: "Questions",
    selected_count: "Selected",
    attempts_total: "Attempts",
    submitted_total: "Submitted",
    avg_score: "Avg Score",
    max_score: "Max Score",
    min_score: "Min Score",
    created_at: "Created At",
    updated_at: "Updated At",
    start_at: "Start At",
    end_at: "End At",
    submitted_at: "Submitted At",
    filename: "File",
    download_url: "Download URL",
    total_count: "Count",
    limit: "Limit",
    offset: "Offset"
  };

  const SUMMARY_KEYS = [
    "title",
    "name",
    "status",
    "effective_status",
    "type",
    "exam_id",
    "attempt_id",
    "practice_exam_id",
    "class_id",
    "student_id",
    "score",
    "total",
    "accuracy",
    "question_count",
    "selected_count",
    "attempts_total",
    "submitted_total",
    "avg_score",
    "created_at",
    "updated_at"
  ];

  const ARRAY_KEYS = ["items", "records", "questions", "attempts", "wrongs", "logs"];
  const SUCCESS_WORDS = /(success|done|completed|created|updated|refreshed|loaded|exported|imported|saved|published|archived|finished)/i;
  const ERROR_WORDS = /(error|failed|invalid|forbidden|not found|denied|mismatch|conflict|missing)/i;
  const WARNING_WORDS = /(warning|notice|confirm|please)/i;

  function isPlainObject(value) {
    return Object.prototype.toString.call(value) === "[object Object]";
  }

  function isEmpty(value) {
    if (value === null || value === undefined) return true;
    if (typeof value === "string") return value.trim() === "";
    if (Array.isArray(value)) return value.length === 0;
    if (isPlainObject(value)) return Object.keys(value).length === 0;
    return false;
  }

  function pad(num) {
    return String(num).padStart(2, "0");
  }

  function formatDateMaybe(value) {
    if (typeof value !== "string") return null;
    const normalized = value.trim();
    if (!/^\d{4}[-/]\d{2}[-/]\d{2}/.test(normalized)) return null;
    const dt = new Date(normalized);
    if (Number.isNaN(dt.getTime())) return null;
    return `${dt.getFullYear()}/${pad(dt.getMonth() + 1)}/${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  }

  function truncate(text, max) {
    const raw = String(text || "");
    const cap = Number.isFinite(max) ? max : 72;
    return raw.length > cap ? `${raw.slice(0, cap - 1)}...` : raw;
  }

  function toDisplay(value) {
    if (value === null || value === undefined || value === "") return "-";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "-";
    if (typeof value === "string") return formatDateMaybe(value) || truncate(value, 96);
    if (Array.isArray(value)) return `Array(${value.length})`;
    if (isPlainObject(value)) return `Object(${Object.keys(value).length})`;
    return truncate(String(value), 96);
  }

  function prettyKey(key) {
    const pure = String(key || "").replace(/^stats\./, "");
    return KEY_LABELS[pure] || pure.replace(/_/g, " ");
  }

  function keyPriority(key) {
    const pure = String(key || "").replace(/^stats\./, "");
    const idx = SUMMARY_KEYS.indexOf(pure);
    return idx === -1 ? Number.MAX_SAFE_INTEGER : idx;
  }

  function collectScalars(obj) {
    const list = [];
    Object.entries(obj || {}).forEach(([key, value]) => {
      if (Array.isArray(value) || isPlainObject(value)) return;
      list.push({ key, value });
    });
    if (isPlainObject(obj && obj.stats)) {
      Object.entries(obj.stats).forEach(([key, value]) => {
        if (Array.isArray(value) || isPlainObject(value)) return;
        list.push({ key: `stats.${key}`, value });
      });
    }
    return list;
  }

  function buildTablePreview(items, label) {
    if (!Array.isArray(items)) return null;
    const tableLabel = label || "List";

    if (!items.length) {
      return { label: tableLabel, count: 0, columns: [], rows: [], primitiveRows: [] };
    }

    const objectRows = items.filter(isPlainObject);
    if (!objectRows.length) {
      return {
        label: tableLabel,
        count: items.length,
        columns: [],
        rows: [],
        primitiveRows: items.slice(0, 6).map((row) => toDisplay(row))
      };
    }

    const sampled = objectRows.slice(0, 6);
    const mergedColumns = [];
    sampled.forEach((row) => {
      Object.keys(row).forEach((key) => {
        if (!mergedColumns.includes(key)) mergedColumns.push(key);
      });
    });
    mergedColumns.sort((a, b) => keyPriority(a) - keyPriority(b));

    const columns = mergedColumns.slice(0, 8);
    const rows = items.slice(0, 6).map((row) => {
      if (!isPlainObject(row)) return columns.map(() => "-");
      return columns.map((column) => toDisplay(row[column]));
    });

    return {
      label: tableLabel,
      count: items.length,
      shown: rows.length,
      columns,
      rows,
      primitiveRows: []
    };
  }

  function summarize(data) {
    if (Array.isArray(data)) {
      return {
        chips: [{ key: "total_count", value: data.length }],
        fields: [],
        table: buildTablePreview(data, "Result List"),
        kind: "array"
      };
    }

    if (isPlainObject(data)) {
      const chips = [];
      const scalarEntries = collectScalars(data);
      scalarEntries.sort((a, b) => keyPriority(a.key) - keyPriority(b.key));

      const usedKeys = new Set();
      SUMMARY_KEYS.forEach((matchKey) => {
        const matched = scalarEntries.find((entry) => entry.key.replace(/^stats\./, "") === matchKey);
        if (!matched || usedKeys.has(matched.key)) return;
        usedKeys.add(matched.key);
        chips.push({ key: matched.key, value: matched.value });
      });

      ARRAY_KEYS.forEach((arrayKey) => {
        if (Array.isArray(data[arrayKey])) chips.push({ key: arrayKey, value: data[arrayKey].length });
      });

      if (typeof data.total === "number" && !chips.some((chip) => chip.key === "total")) {
        chips.push({ key: "total", value: data.total });
      }
      if (typeof data.limit === "number") chips.push({ key: "limit", value: data.limit });
      if (typeof data.offset === "number") chips.push({ key: "offset", value: data.offset });

      const fields = scalarEntries
        .filter((entry) => !usedKeys.has(entry.key))
        .slice(0, 12)
        .map((entry) => ({ key: entry.key, value: entry.value }));

      let table = null;
      const tableSourceKey = ARRAY_KEYS.find((key) => Array.isArray(data[key]));
      if (tableSourceKey) {
        table = buildTablePreview(data[tableSourceKey], prettyKey(tableSourceKey));
      }

      return { chips, fields, table, kind: "object" };
    }

    return {
      chips: [],
      fields: [{ key: "value", value: data }],
      table: null,
      kind: "scalar"
    };
  }

  function createElement(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatRawJson(data) {
    try {
      return JSON.stringify(data, null, 2);
    } catch (_) {
      return String(data);
    }
  }

  function renderHeader(shell, title, kind) {
    const head = createElement("div", "cx-smart-head");
    const heading = createElement("h4", "cx-smart-title", title || "Result Overview");
    const metaText = kind === "array" ? "Array payload" : (kind === "object" ? "Object payload" : "Scalar payload");
    const meta = createElement("span", "cx-smart-meta", metaText);
    head.appendChild(heading);
    head.appendChild(meta);
    shell.appendChild(head);
  }

  function renderChips(shell, chips) {
    if (!chips.length) return;
    const row = createElement("div", "cx-smart-chips");
    chips.slice(0, 10).forEach((chip) => {
      const item = createElement("span", "cx-smart-chip");
      item.textContent = `${prettyKey(chip.key)}: ${toDisplay(chip.value)}`;
      row.appendChild(item);
    });
    shell.appendChild(row);
  }

  function renderFields(shell, fields) {
    if (!fields.length) return;
    const grid = createElement("div", "cx-smart-grid");
    fields.forEach((field) => {
      const item = createElement("div", "cx-smart-kv");
      const label = createElement("span", "cx-smart-k", prettyKey(field.key));
      const value = createElement("strong", "cx-smart-v", toDisplay(field.value));
      item.appendChild(label);
      item.appendChild(value);
      grid.appendChild(item);
    });
    shell.appendChild(grid);
  }

  function renderTable(shell, table) {
    if (!table) return;

    const box = createElement("div", "cx-smart-table-box");
    box.appendChild(createElement("div", "cx-smart-table-title", `${table.label} (total ${table.count})`));

    if (!table.count) {
      box.appendChild(createElement("div", "cx-smart-empty-lite", "No list rows for current filters"));
      shell.appendChild(box);
      return;
    }

    if (table.primitiveRows && table.primitiveRows.length) {
      const list = createElement("ul", "cx-smart-list");
      table.primitiveRows.forEach((row) => list.appendChild(createElement("li", "", row)));
      box.appendChild(list);
      shell.appendChild(box);
      return;
    }

    const scroll = createElement("div", "cx-smart-table-scroll");
    const tableEl = createElement("table", "cx-smart-table");
    const thead = createElement("thead");
    const headRow = createElement("tr");

    table.columns.forEach((column) => {
      headRow.appendChild(createElement("th", "", prettyKey(column)));
    });

    thead.appendChild(headRow);

    const tbody = createElement("tbody");
    table.rows.forEach((row) => {
      const tr = createElement("tr");
      row.forEach((cell) => tr.appendChild(createElement("td", "", cell)));
      tbody.appendChild(tr);
    });

    tableEl.appendChild(thead);
    tableEl.appendChild(tbody);
    scroll.appendChild(tableEl);
    box.appendChild(scroll);
    shell.appendChild(box);
  }

  function renderRaw(shell, data) {
    const details = createElement("details", "cx-smart-raw");
    const summary = createElement("summary", "", "View raw JSON");
    const pre = createElement("pre", "cx-smart-pre", formatRawJson(data));
    details.appendChild(summary);
    details.appendChild(pre);
    shell.appendChild(details);
  }

  function renderEmpty(target, emptyText) {
    target.innerHTML = "";
    target.classList.add("cx-smart-output", "is-empty");
    const text = emptyText || target.dataset.empty || "No data";
    target.appendChild(createElement("div", "cx-smart-empty", text));
  }

  function render(target, data, options) {
    if (!target) return;
    const opts = options || {};

    if (isEmpty(data)) {
      renderEmpty(target, opts.emptyText);
      return;
    }

    const summary = summarize(data);
    target.innerHTML = "";
    target.classList.add("cx-smart-output");
    target.classList.remove("is-empty");

    const shell = createElement("div", "cx-smart-shell");
    renderHeader(shell, opts.title, summary.kind);
    renderChips(shell, summary.chips);
    renderFields(shell, summary.fields);
    renderTable(shell, summary.table);
    renderRaw(shell, data);
    target.appendChild(shell);
  }

  function currentTime() {
    const now = new Date();
    return `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }

  function inferTone(message, ok) {
    if (ok === true) return "success";
    if (ok === false) return "error";

    const text = String(message || "");
    if (ERROR_WORDS.test(text)) return "error";
    if (SUCCESS_WORDS.test(text)) return "success";
    if (WARNING_WORDS.test(text)) return "warning";
    return "info";
  }

  function renderStatus(target, text, ok) {
    if (!target) return;

    const message = String(text || "").trim();
    target.innerHTML = "";
    target.className = "status";
    if (!message) return;

    const tone = inferTone(message, ok);
    target.classList.add("has-message", `is-${tone}`);

    const iconText = tone === "success" ? "OK" : (tone === "error" ? "!" : (tone === "warning" ? "!" : "i"));
    const icon = createElement("span", "status-icon", iconText);
    icon.setAttribute("aria-hidden", "true");

    const main = createElement("span", "status-main", message);
    const time = createElement("time", "status-time", currentTime());

    target.appendChild(icon);
    target.appendChild(main);
    target.appendChild(time);
  }

  function compact(value, maxLength) {
    const limit = Number.isFinite(maxLength) ? maxLength : 70;
    if (value === null || value === undefined || value === "") return "-";
    if (typeof value === "string") return truncate(value, limit);
    if (typeof value === "number" || typeof value === "boolean") return String(value);

    if (Array.isArray(value)) {
      if (!value.length) return "Empty array";
      return `Array(${value.length}) ${truncate(toDisplay(value[0]), 24)}`;
    }

    if (isPlainObject(value)) {
      const keys = Object.keys(value);
      if (!keys.length) return "Empty object";
      const summary = keys.slice(0, 3).map((key) => `${prettyKey(key)}:${toDisplay(value[key])}`).join(" | ");
      return truncate(summary, limit);
    }

    return truncate(String(value), limit);
  }

  function ensureDialogRoot() {
    let root = document.getElementById("cx-dialog-root");
    if (root) return root;
    root = createElement("div", "cx-dialog-root");
    root.id = "cx-dialog-root";
    root.innerHTML = [
      '<div class="cx-dialog-backdrop" data-role="backdrop"></div>',
      '<div class="cx-dialog-panel" role="dialog" aria-modal="true" aria-labelledby="cx-dialog-title" tabindex="-1">',
      '<h3 id="cx-dialog-title" class="cx-dialog-title"></h3>',
      '<p class="cx-dialog-message" data-role="message"></p>',
      '<input class="cx-dialog-input" data-role="input" />',
      '<div class="cx-dialog-actions">',
      '<button type="button" class="btn secondary" data-role="cancel"></button>',
      '<button type="button" class="btn" data-role="ok"></button>',
      "</div>",
      "</div>"
    ].join("");
    document.body.appendChild(root);
    return root;
  }

  function openDialog(options) {
    const opts = options || {};
    const root = ensureDialogRoot();
    const backdrop = root.querySelector('[data-role="backdrop"]');
    const titleEl = root.querySelector("#cx-dialog-title");
    const messageEl = root.querySelector('[data-role="message"]');
    const inputEl = root.querySelector('[data-role="input"]');
    const cancelBtn = root.querySelector('[data-role="cancel"]');
    const okBtn = root.querySelector('[data-role="ok"]');
    const panel = root.querySelector(".cx-dialog-panel");

    titleEl.textContent = opts.title || "Confirm";
    messageEl.textContent = opts.message || "";
    cancelBtn.textContent = opts.cancelText || "Cancel";
    okBtn.textContent = opts.okText || "OK";

    const isPrompt = opts.type === "prompt";
    inputEl.style.display = isPrompt ? "block" : "none";
    if (isPrompt) {
      inputEl.type = opts.password ? "password" : "text";
      inputEl.placeholder = opts.placeholder || "";
      inputEl.value = opts.defaultValue || "";
    } else {
      inputEl.value = "";
    }

    root.classList.add("open");
    panel.focus();
    if (isPrompt) setTimeout(() => inputEl.focus(), 0);

    return new Promise((resolve) => {
      let done = false;
      const finish = (value) => {
        if (done) return;
        done = true;
        root.classList.remove("open");
        cleanup();
        resolve(value);
      };
      const onCancel = () => finish(null);
      const onConfirm = () => finish(isPrompt ? inputEl.value : true);
      const onKeyDown = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onCancel();
          return;
        }
        if (event.key === "Enter") {
          event.preventDefault();
          onConfirm();
        }
      };
      const cleanup = () => {
        backdrop.removeEventListener("click", onCancel);
        cancelBtn.removeEventListener("click", onCancel);
        okBtn.removeEventListener("click", onConfirm);
        root.removeEventListener("keydown", onKeyDown);
      };

      backdrop.addEventListener("click", onCancel);
      cancelBtn.addEventListener("click", onCancel);
      okBtn.addEventListener("click", onConfirm);
      root.addEventListener("keydown", onKeyDown);
    });
  }

  function dialogConfirm(message, options) {
    return openDialog({
      type: "confirm",
      title: (options && options.title) || "请确认",
      message: message || "",
      okText: (options && options.okText) || "确认",
      cancelText: (options && options.cancelText) || "取消"
    }).then((value) => Boolean(value));
  }

  function dialogPrompt(message, options) {
    const opts = options || {};
    return openDialog({
      type: "prompt",
      title: opts.title || "请输入",
      message: message || "",
      placeholder: opts.placeholder || "",
      defaultValue: opts.defaultValue || "",
      okText: opts.okText || "确认",
      cancelText: opts.cancelText || "取消",
      password: Boolean(opts.password)
    });
  }

  global.CXSmartOutput = {
    render,
    renderEmpty,
    renderStatus,
    compact,
    confirm: dialogConfirm,
    prompt: dialogPrompt,
    raw: formatRawJson
  };
})(window);
