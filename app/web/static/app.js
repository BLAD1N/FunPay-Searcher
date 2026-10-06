/* ============================================================================
   FunPay Searcher — клиентское приложение (SPA без сборки, ES2020).

   Разделы файла:
     1. Утилиты (DOM-конструктор, форматирование, работа с путями объекта)
     2. API (обёртка над fetch, ошибки {detail})
     3. Уведомления, модальные окна, подтверждения
     4. Общие компоненты (чипы статусов, индикаторы, редакторы тегов/пар/строк)
     5. Роутер и боковая панель
     6. Страницы: панель, найдено, лоты, профили, редактор профиля, настройки, журнал
   ============================================================================ */
'use strict';

// ----------------------------------------------------------------------------
// 1. Утилиты
// ----------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);

/** Создание DOM-элемента: h('div', {class: 'x', onclick: fn}, 'текст', child, [children]). */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  let deferredValue;
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style') { if (typeof v === 'string') el.style.cssText = v; else Object.assign(el.style, v); }
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'value') deferredValue = v;          // для <select> — после добавления <option>
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (typeof v === 'boolean') { if (k in el) el[k] = v; else el.setAttribute(k, ''); }
      else el.setAttribute(k, v);
    }
  }
  append(el, ...children);
  if (deferredValue !== undefined) el.value = deferredValue;
  return el;
}

function append(el, ...children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false || c === true) continue;
    el.append(c instanceof Node ? c : String(c));
  }
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function replace(el, ...children) { clear(el); append(el, ...children); return el; }

const nfRU = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const nfRU2 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
const CUR_SYMBOL = { RUB: '₽', USD: '$', EUR: '€', UAH: '₴', KZT: '₸' };

/** Деньги: 15000 -> "15 000 ₽" (неразрывные пробелы). */
function fmtMoney(v, cur = 'RUB') {
  if (v == null || v === '' || isNaN(Number(v))) return '—';
  const sym = CUR_SYMBOL[cur] || cur || '';
  return nfRU.format(Math.round(Number(v))).replace(/[   ]/g, ' ') + ' ' + sym;
}
function fmtNum(v) { return v == null || v === '' || isNaN(Number(v)) ? '—' : nfRU2.format(Number(v)).replace(/[  ]/g, ' '); }
function fmtPct(v) { return v == null || !isFinite(v) ? '—' : (v > 0 ? '+' : '') + nfRU.format(Math.round(v)) + '%'; }

function parseDate(v) {
  if (!v) return null;
  const d = v instanceof Date ? v : new Date(v);
  return isNaN(d.getTime()) ? null : d;
}
/** Дата и время: "06.10.2026, 14:05". */
function fmtDate(v) {
  const d = parseDate(v);
  if (!d) return '—';
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtTime(v) {
  const d = parseDate(v);
  return d ? d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
}
/** Относительное время: "5 мин назад", "через 10 мин". */
function fmtRel(v) {
  const d = parseDate(v);
  if (!d) return '—';
  const diff = (Date.now() - d.getTime()) / 1000;
  const abs = Math.abs(diff);
  const past = diff >= 0;
  let text;
  if (abs < 45) text = 'только что';
  else if (abs < 3600) text = Math.round(abs / 60) + ' мин';
  else if (abs < 86400) text = Math.round(abs / 3600) + ' ч';
  else if (abs < 7 * 86400) text = Math.round(abs / 86400) + ' дн.';
  else return fmtDate(d);
  if (text === 'только что') return text;
  return past ? text + ' назад' : 'через ' + text;
}
function fmtDuration(a, b) {
  const da = parseDate(a), db = parseDate(b);
  if (!da || !db) return '—';
  const s = Math.max(0, Math.round((db - da) / 1000));
  return s < 60 ? s + ' с' : Math.floor(s / 60) + ' мин ' + (s % 60) + ' с';
}
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function clone(o) { return JSON.parse(JSON.stringify(o)); }
function isToday(v) { const d = parseDate(v); if (!d) return false; const n = new Date(); return d.toDateString() === n.toDateString(); }

/** Доступ к вложенному полю по пути 'a.b.c'. */
function getPath(obj, path) { return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj); }
function setPath(obj, path, value) {
  const keys = path.split('.');
  let o = obj;
  for (const k of keys.slice(0, -1)) { if (o[k] == null || typeof o[k] !== 'object') o[k] = {}; o = o[k]; }
  o[keys[keys.length - 1]] = value;
}
function toNum(v) { if (v === '' || v == null) return null; const n = Number(String(v).replace(',', '.')); return isNaN(n) ? null : n; }

/** Запуск асинхронного действия с блокировкой кнопки и показом ошибки. */
async function busy(btn, fn) {
  if (btn && btn.disabled) return;
  const old = btn ? btn.innerHTML : null;
  if (btn) { btn.disabled = true; btn.prepend(h('span', { class: 'spinner' })); }
  try { return await fn(); }
  catch (e) { toast(e.message || String(e), 'error'); }
  finally { if (btn) { btn.disabled = false; btn.innerHTML = old; } }
}

// ----------------------------------------------------------------------------
// 2. API
// ----------------------------------------------------------------------------

async function request(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opts); }
  catch (e) { throw new Error('Нет связи с сервером приложения'); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
  if (!res.ok) {
    let msg = `Ошибка ${res.status}`;
    const d = data && data.detail;
    if (typeof d === 'string') msg = d;
    else if (Array.isArray(d)) msg = d.map(x => `${(x.loc || []).filter(p => p !== 'body').join('.')}: ${x.msg}`).join('; ');
    else if (d) msg = JSON.stringify(d);
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return data;
}
const API = {
  get: (url) => request('GET', url),
  post: (url, body = {}) => request('POST', url, body),
  put: (url, body) => request('PUT', url, body),
  del: (url) => request('DELETE', url),
};
function qs(params) {
  const p = Object.entries(params).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  return p.length ? '?' + p.join('&') : '';
}

// ----------------------------------------------------------------------------
// 3. Уведомления, модальные окна, подтверждения
// ----------------------------------------------------------------------------

function toast(message, type = 'info', ms = 4500) {
  const root = $('#toasts');
  const el = h('div', { class: 'toast ' + type, role: type === 'error' ? 'alert' : 'status' },
    h('span', {}, message),
    h('button', { class: 'toast-close', 'aria-label': 'Закрыть', onclick: () => hide() }, '×'));
  const hide = () => { el.classList.add('hide'); setTimeout(() => el.remove(), 220); };
  root.append(el);
  setTimeout(hide, type === 'error' ? ms + 3000 : ms);
}

/** Модальное окно. Возвращает {close, body, footer}. */
function openModal({ title, body, footer, size = '', onClose }) {
  const root = $('#modal-root');
  const bodyEl = h('div', { class: 'modal-body' }, body);
  const footerEl = h('div', { class: 'modal-footer' }, footer);
  if (!footer) footerEl.hidden = true;
  const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); if (onClose) onClose(); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  const modal = h('div', { class: 'modal ' + size, role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-header' }, h('h2', {}, title), h('button', { class: 'modal-close', 'aria-label': 'Закрыть', onclick: close }, '×')),
    bodyEl, footerEl);
  const backdrop = h('div', { class: 'modal-backdrop', onmousedown: (e) => { if (e.target === backdrop) close(); } }, modal);
  root.append(backdrop);
  document.addEventListener('keydown', onKey);
  const first = modal.querySelector('input, textarea, select, button.btn-primary, button');
  if (first) setTimeout(() => first.focus(), 30);
  return { close, body: bodyEl, footer: footerEl, el: modal };
}

/** Диалог подтверждения: await confirmDialog('Удалить?') -> true/false. */
function confirmDialog(text, { title = 'Подтверждение', ok = 'Да', danger = true } = {}) {
  return new Promise((resolve) => {
    const m = openModal({
      title, size: 'sm', body: h('p', {}, text), onClose: () => resolve(false),
      footer: [
        h('button', { class: 'btn', onclick: () => m.close() }, 'Отмена'),
        h('button', { class: 'btn ' + (danger ? 'btn-danger' : 'btn-primary'), onclick: () => { resolve(true); m.close(); } }, ok),
      ],
    });
  });
}

/** Окно с «сырым» JSON. */
function showJsonModal(title, data) {
  openModal({ title, size: 'lg', body: h('pre', { class: 'result-box mono' }, JSON.stringify(data, null, 2)) });
}

// ----------------------------------------------------------------------------
// 4. Общие компоненты
// ----------------------------------------------------------------------------

const state = { status: null, profiles: [], funpayCategories: null, lolzCategories: null, pollTimer: null, pageToken: 0 };

const FOUND_STATUS = {
  new: { label: 'Новое', cls: 'accent' }, candidate: { label: 'Кандидат', cls: 'success' },
  rejected: { label: 'Отклонено', cls: 'neutral' }, ignored: { label: 'Скрыто', cls: 'neutral' },
  published: { label: 'Опубликовано', cls: 'accent' }, sold: { label: 'Продано', cls: 'warning' },
};
const LOT_STATUS = {
  draft: { label: 'Черновик', cls: 'neutral' }, active: { label: 'Активен', cls: 'success' },
  deactivated: { label: 'Снят', cls: 'warning' }, sold: { label: 'Продан', cls: 'warning' }, error: { label: 'Ошибка', cls: 'danger' },
};
const SOURCE_LABEL = { funpay: 'FunPay', lolz: 'Lolz' };

function statusChip(status, map) {
  const s = map[status] || { label: status || '—', cls: 'neutral' };
  return h('span', { class: 'chip ' + s.cls }, s.label);
}
function sourceChip(source) { return h('span', { class: 'chip ' + source }, SOURCE_LABEL[source] || source); }

/** Индикатор доступности исходника: зелёный/красный/серый + время проверки. */
function availIndicator(available, checkedAt, { prefix = '' } = {}) {
  const cls = available === true ? 'ok' : available === false ? 'bad' : 'unknown';
  const label = available === true ? 'доступен' : available === false ? 'недоступен' : 'не проверялся';
  return h('span', { class: 'avail', title: checkedAt ? 'Проверено ' + fmtDate(checkedAt) : '' },
    h('span', { class: 'dot ' + cls }), h('span', {}, prefix + label), checkedAt ? h('span', { class: 'avail-when' }, '· ' + fmtRel(checkedAt)) : null);
}
function emptyState(title, text, icon = '◌') {
  return h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon), h('div', { class: 'empty-title' }, title), text ? h('div', {}, text) : null);
}
function loadingState() { return h('div', { class: 'page-loading' }, h('span', { class: 'spinner' }), ' Загрузка…'); }
function extLink(href, text) { return href ? h('a', { href, target: '_blank', rel: 'noopener' }, text) : null; }

function field(label, control, hint, cls = '') {
  return h('div', { class: 'field ' + cls }, label ? h('label', {}, label) : null, control, hint ? h('div', { class: 'hint' }, hint) : null);
}
/** Привязанные к модели поля ввода (model + путь 'a.b.c'). */
function textInput(model, path, opts = {}) {
  return h('input', { class: 'input ' + (opts.class || ''), type: opts.type || 'text', placeholder: opts.placeholder, readonly: opts.readonly, value: getPath(model, path) ?? '',
    oninput: (e) => setPath(model, path, opts.nullable && e.target.value === '' ? null : e.target.value) });
}
function numberInput(model, path, opts = {}) {
  return h('input', { class: 'input ' + (opts.class || ''), type: 'number', step: opts.step || 'any', min: opts.min, placeholder: opts.placeholder, value: getPath(model, path) ?? '',
    oninput: (e) => setPath(model, path, toNum(e.target.value)) });
}
function selectInput(model, path, options, opts = {}) {
  return h('select', { class: 'select ' + (opts.class || ''), value: String(getPath(model, path) ?? ''),
    onchange: (e) => { const v = e.target.value; setPath(model, path, v === '' ? null : v); if (opts.onchange) opts.onchange(v); } },
    options.map(o => h('option', { value: String(o.value ?? '') }, o.label)));
}
function textareaInput(model, path, opts = {}) {
  return h('textarea', { class: 'textarea ' + (opts.class || ''), rows: opts.rows || 4, placeholder: opts.placeholder, value: getPath(model, path) ?? '',
    oninput: (e) => setPath(model, path, e.target.value) });
}
function checkInput(model, path, label, opts = {}) {
  return h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: !!getPath(model, path),
    onchange: (e) => { setPath(model, path, e.target.checked); if (opts.onchange) opts.onchange(e.target.checked); } }), label);
}
function switchInput(checked, label, onchange) {
  return h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: !!checked, onchange: (e) => onchange(e.target.checked) }), h('span', { class: 'track' }), label ? h('span', {}, label) : null);
}

/** Ввод тегов: работает с массивом «на месте» (push/splice), чтобы модель оставалась связанной. */
function tagInput(list, { placeholder = 'Добавить и нажать Enter', mono = false } = {}) {
  const box = h('div', { class: 'tags', onclick: () => input.focus() });
  const input = h('input', { placeholder, onkeydown: (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(input.value); }
    else if (e.key === 'Backspace' && !input.value && list.length) { list.pop(); render(); }
  }, onblur: () => add(input.value) });
  const add = (raw) => { const v = raw.trim().replace(/,$/, ''); if (!v) return; if (!list.includes(v)) list.push(v); input.value = ''; render(); };
  const render = () => replace(box,
    list.map((t, i) => h('span', { class: 'tag' + (mono ? ' mono' : '') }, t, h('button', { type: 'button', class: 'tag-x', 'aria-label': 'Удалить ' + t, onclick: (e) => { e.stopPropagation(); list.splice(i, 1); render(); } }, '×'))),
    input);
  render();
  return box;
}

/** Редактор пар «ключ — значение»: переписывает объект obj на месте. */
function kvEditor(obj, { keyPh = 'ключ', valPh = 'значение', parse = (v) => v, format = (v) => (typeof v === 'string' ? v : JSON.stringify(v)), addLabel = 'Добавить' } = {}) {
  const rows = Object.entries(obj).map(([k, v]) => ({ k, v: format(v) }));
  const box = h('div', { class: 'kv' });
  const sync = () => { for (const k of Object.keys(obj)) delete obj[k]; for (const r of rows) if (r.k.trim()) obj[r.k.trim()] = parse(r.v); };
  const render = () => replace(box,
    rows.length ? h('div', { class: 'kv-row kv-head' }, h('span', {}, keyPh), h('span', {}, valPh), h('span')) : null,
    rows.map((r, i) => h('div', { class: 'kv-row' },
      h('input', { class: 'input sm', value: r.k, placeholder: keyPh, oninput: (e) => { r.k = e.target.value; sync(); } }),
      h('input', { class: 'input sm', value: r.v, placeholder: valPh, oninput: (e) => { r.v = e.target.value; sync(); } }),
      h('button', { type: 'button', class: 'btn btn-ghost btn-icon', 'aria-label': 'Удалить строку', onclick: () => { rows.splice(i, 1); sync(); render(); } }, '×'))),
    h('div', {}, h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { rows.push({ k: '', v: '' }); render(); box.querySelector('.kv-row:last-of-type input')?.focus(); } }, '+ ' + addLabel)));
  render();
  box.refresh = () => { rows.length = 0; rows.push(...Object.entries(obj).map(([k, v]) => ({ k, v: format(v) }))); render(); };
  return box;
}

/** Редактор списка объектов (строки с колонками), работает с массивом на месте. */
function rowsEditor(list, columns, { addLabel = 'Добавить строку', blank = () => ({}) } = {}) {
  const box = h('div', { class: 'kv' });
  const render = () => replace(box,
    list.length ? h('div', { class: 'kv-row kv-head cols-' + columns.length }, columns.map(c => h('span', {}, c.label)), h('span')) : null,
    list.map((row, i) => h('div', { class: 'kv-row cols-' + columns.length },
      columns.map(c => h('input', { class: 'input sm', type: c.type || 'text', step: c.type === 'number' ? 'any' : null, placeholder: c.placeholder || '', value: row[c.key] ?? '',
        oninput: (e) => { row[c.key] = c.type === 'number' ? toNum(e.target.value) : e.target.value; if (c.type === 'number' && row[c.key] == null) delete row[c.key]; } })),
      h('button', { type: 'button', class: 'btn btn-ghost btn-icon', 'aria-label': 'Удалить строку', onclick: () => { list.splice(i, 1); render(); } }, '×'))),
    h('div', {}, h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { list.push(blank()); render(); } }, '+ ' + addLabel)));
  render();
  return box;
}

/** Переключаемые чипы (мультивыбор) для набора значений. */
function chipToggles(options, selected, onchange) {
  return h('div', { class: 'chip-row' }, options.map(o => {
    const chip = h('button', { type: 'button', class: 'chip toggle' + (selected.has(o.value) ? ' on' : ''), 'aria-pressed': selected.has(o.value) ? 'true' : 'false',
      onclick: () => { selected.has(o.value) ? selected.delete(o.value) : selected.add(o.value); chip.classList.toggle('on'); chip.setAttribute('aria-pressed', chip.classList.contains('on')); onchange(); } }, o.label);
    return chip;
  }));
}

/** Форма лота (общая для предпросмотра и редактирования). Возвращает {el, values()}. */
function lotForm(lot) {
  const m = { title_ru: lot.title_ru || '', title_en: lot.title_en || '', description_ru: lot.description_ru || '', description_en: lot.description_en || '', price: lot.price ?? 0, fields: clone(lot.fields || {}) };
  const el = h('div', { class: 'stack' },
    h('div', { class: 'form-grid' },
      field('Заголовок (RU)', textInput(m, 'title_ru')),
      field('Заголовок (EN)', textInput(m, 'title_en')),
      field('Описание (RU)', textareaInput(m, 'description_ru', { rows: 6 }), null, 'span-2'),
      field('Описание (EN)', textareaInput(m, 'description_en', { rows: 4 }), null, 'span-2'),
      field('Цена, ₽', numberInput(m, 'price', { min: 0 }), lot.source_price ? 'Цена исходника: ' + fmtMoney(lot.source_price) : null),
      field('Подкатегория FunPay', h('input', { class: 'input', readonly: true, value: lot.subcategory_id ?? '—' })),
    ),
    field('Дополнительные поля формы FunPay', kvEditor(m.fields, { keyPh: 'fields[...]', valPh: 'значение', addLabel: 'Добавить поле' })),
  );
  return { el, values: () => ({ ...m, price: toNum(m.price) ?? 0 }) };
}

/** Модалка предпросмотра/создания лота для найденного объявления. */
async function openLotPreview(found, afterSave) {
  let preview;
  try { preview = await API.post(`/api/found/${found.id}/preview-lot`); }
  catch (e) { toast(e.message, 'error'); return; }
  const form = lotForm(preview);
  const l = found.listing;
  const m = openModal({
    title: 'Предпросмотр лота', size: 'lg',
    body: [
      h('div', { class: 'callout info mb-16' }, h('div', {}, h('b', {}, 'Исходник: '), extLink(l.url, l.title || l.url), ' — ', fmtMoney(l.price, l.currency),
        l.seller_name ? [' · продавец ', extLink(l.seller_url, l.seller_name) || l.seller_name] : null)),
      form.el,
    ],
    footer: [
      h('button', { class: 'btn', onclick: () => m.close() }, 'Отмена'),
      h('button', { class: 'btn', onclick: (e) => busy(e.currentTarget, async () => {
        const lot = await API.post(`/api/found/${found.id}/create-lot`, { ...form.values(), publish: false });
        toast(`Черновик лота #${lot.id} сохранён`, 'success'); m.close(); if (afterSave) afterSave(lot);
      }) }, 'Сохранить черновик'),
      h('button', { class: 'btn btn-primary', onclick: (e) => busy(e.currentTarget, async () => {
        if (!(await confirmDialog('Создать и опубликовать лот на FunPay прямо сейчас?', { ok: 'Создать на FunPay', danger: false }))) return;
        const lot = await API.post(`/api/found/${found.id}/create-lot`, { ...form.values(), publish: true });
        toast(lot.status === 'active' ? 'Лот создан на FunPay' : `Лот #${lot.id}: ${LOT_STATUS[lot.status]?.label || lot.status}${lot.error ? ' — ' + lot.error : ''}`, lot.status === 'error' ? 'error' : 'success');
        m.close(); if (afterSave) afterSave(lot);
      }) }, 'Создать на FunPay'),
    ],
  });
}

/** Модалка редактирования нашего лота. */
function openLotEdit(lot, afterSave) {
  const form = lotForm(lot);
  const m = openModal({
    title: `Редактировать лот #${lot.id}`, size: 'lg', body: form.el,
    footer: [
      h('button', { class: 'btn', onclick: () => m.close() }, 'Отмена'),
      h('button', { class: 'btn btn-primary', onclick: (e) => busy(e.currentTarget, async () => {
        const updated = await API.put(`/api/lots/${lot.id}`, form.values());
        toast('Лот сохранён', 'success'); m.close(); if (afterSave) afterSave(updated);
      }) }, 'Сохранить'),
    ],
  });
}

/** Выбор подкатегории FunPay из дерева категорий. */
async function pickFunPayCategory(onPick) {
  let cats = state.funpayCategories;
  if (!cats) {
    try { cats = state.funpayCategories = await API.get('/api/funpay/categories'); }
    catch (e) { toast('Не удалось загрузить категории FunPay: ' + e.message, 'error'); return; }
  }
  const list = h('div', { class: 'picker-list' });
  const render = (q) => {
    q = (q || '').trim().toLowerCase();
    const groups = [];
    for (const g of cats) {
      const subs = (g.subcategories || []).filter(s => !q || (g.name + ' ' + s.name + ' ' + s.id).toLowerCase().includes(q));
      if (subs.length) groups.push({ g, subs });
    }
    replace(list, groups.length ? groups.slice(0, 300).map(({ g, subs }) => [
      h('div', { class: 'picker-group' }, g.name),
      subs.map(s => h('button', { type: 'button', class: 'picker-item', onclick: () => { onPick({ id: s.id, name: s.name, game: g.name, type: s.type }); m.close(); } },
        s.name, s.type === 'currency' ? h('span', { class: 'chip soft' }, 'валюта') : null, h('span', { class: 'id' }, '#' + s.id))),
    ]) : emptyState('Ничего не найдено'));
  };
  const search = h('input', { class: 'input mb-8', placeholder: 'Поиск по игре или разделу…', oninput: (e) => render(e.target.value) });
  const m = openModal({ title: 'Выбрать категорию FunPay', size: 'lg', body: [search, list] });
  render('');
}
function funpayCategoryName(id) {
  if (!state.funpayCategories || id == null) return null;
  for (const g of state.funpayCategories) for (const s of g.subcategories || []) if (String(s.id) === String(id)) return `${g.name} → ${s.name}`;
  return null;
}

// ----------------------------------------------------------------------------
// 5. Роутер и боковая панель
// ----------------------------------------------------------------------------

const ROUTES = [
  { re: /^#\/dashboard\/?$/, nav: 'dashboard', page: pageDashboard },
  { re: /^#\/found\/?$/, nav: 'found', page: pageFound },
  { re: /^#\/lots\/?$/, nav: 'lots', page: pageLots },
  { re: /^#\/profiles\/?$/, nav: 'profiles', page: pageProfiles },
  { re: /^#\/profiles\/([^/]+)\/?$/, nav: 'profiles', page: pageProfileEditor },
  { re: /^#\/settings\/?$/, nav: 'settings', page: pageSettings },
  { re: /^#\/log\/?$/, nav: 'log', page: pageLog },
];

function stopPolling() { if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; } }
/** Периодический опрос (только текущая страница): fn вызывается каждые ms. */
function startPolling(fn, ms) { stopPolling(); state.pollTimer = setInterval(() => { if (!document.hidden) fn().catch(() => {}); }, ms); }

async function navigate() {
  const hash = location.hash || '#/dashboard';
  const route = ROUTES.find(r => r.re.test(hash));
  if (!route) { location.hash = '#/dashboard'; return; }
  const param = decodeURIComponent((hash.match(route.re)[1] || ''));
  stopPolling();
  const token = ++state.pageToken;
  document.querySelectorAll('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.route === route.nav));
  const main = $('#main');
  replace(main, loadingState());
  try {
    const content = h('div', { class: 'page' });
    await route.page(content, param, token);
    if (token !== state.pageToken) return;      // пользователь уже ушёл на другую страницу
    replace(main, content);
  } catch (e) {
    if (token !== state.pageToken) return;
    replace(main, h('div', { class: 'card' }, emptyState('Не удалось загрузить страницу', e.message, '⚠'), h('div', { class: 'row', style: 'justify-content:center' }, h('button', { class: 'btn', onclick: navigate }, 'Повторить'))));
  }
  loadStatus().catch(() => {});
}

async function loadStatus() {
  const st = await API.get('/api/status');
  state.status = st;
  updateSidebar(st);
  return st;
}
function updateSidebar(st) {
  const brand = st.brand || 'FunPay Searcher';
  $('#brand-name').textContent = brand;
  document.title = brand;
  $('#app-version').textContent = st.version ? 'v' + st.version : '';
  const f = (st.counts && st.counts.found) || {}, l = (st.counts && st.counts.lots) || {};
  setBadge('#badge-candidates', (f.candidate || 0) + (f.new || 0));
  setBadge('#badge-lots', l.active || 0);
  setConn('#conn-funpay', 'FunPay', st.auth && st.auth.funpay);
  setConn('#conn-lolz', 'Lolz', st.auth && st.auth.lolz);
}
function setBadge(sel, n) { const el = $(sel); el.textContent = n; el.hidden = !n; }
function setConn(sel, name, a) {
  const el = $(sel);
  el.classList.remove('ok', 'err');
  const user = $(sel + '-user');
  if (!a || a.ok == null) { el.title = name + ': подключение не проверялось'; user.textContent = ''; return; }
  el.classList.add(a.ok ? 'ok' : 'err');
  el.title = a.ok ? `${name}: подключено${a.username ? ' как ' + a.username : ''}` : `${name}: ошибка — ${a.error || 'нет доступа'}`;
  user.textContent = a.ok ? (a.username || 'ок') : 'ошибка';
}
async function loadProfiles() { state.profiles = await API.get('/api/profiles'); return state.profiles; }
function profileName(id) { const p = state.profiles.find(x => x.id === id); return p ? p.name : id; }

// ----------------------------------------------------------------------------
// 6.1 Панель (dashboard)
// ----------------------------------------------------------------------------

async function pageDashboard(root) {
  const [status, profiles] = await Promise.all([loadStatus(), loadProfiles()]);
  const sel = { profiles: new Set(), sources: new Set() };    // пусто = все
  let extra = { found: [], lots: [], events: [] };
  let wasRunning = !!(status.search && status.search.running);

  const progressBox = h('div'), tilesBox = h('div', { class: 'grid grid-4' }), lastBox = h('div'), monitorBox = h('div'), eventsBox = h('div');

  // --- выпадающие списки выбора профилей и источников ---
  const dropdown = (label, options, set) => {
    const summary = h('summary', { class: 'btn' });
    const refresh = () => { summary.textContent = `${label}: ${set.size ? set.size + ' из ' + options.length : 'все'} ▾`; };
    refresh();
    return h('details', { class: 'dropdown' }, summary,
      h('div', { class: 'dropdown-panel' }, options.length ? options.map(o => h('label', { class: 'check' },
        h('input', { type: 'checkbox', checked: set.has(o.value), onchange: (e) => { e.target.checked ? set.add(o.value) : set.delete(o.value); refresh(); } }), o.label)) : h('div', { class: 'muted small' }, 'Нет профилей')));
  };
  const profileOpts = profiles.map(p => ({ value: p.id, label: p.name + (p.enabled ? '' : ' (выкл.)') }));
  const sourceOpts = [{ value: 'funpay', label: 'FunPay' }, { value: 'lolz', label: 'Lolz' }];

  const startBtn = h('button', { class: 'btn btn-primary btn-xl', onclick: (e) => busy(e.currentTarget, async () => {
    await API.post('/api/search', { profile_ids: sel.profiles.size ? [...sel.profiles] : null, sources: sel.sources.size ? [...sel.sources] : null });
    toast('Поиск запущен', 'success');
    await tick();
  }) }, '🔍 Найти аккаунты');

  const hero = h('div', { class: 'card' },
    h('div', { class: 'search-hero' }, startBtn,
      h('div', { class: 'opts' }, dropdown('Профили', profileOpts, sel.profiles), dropdown('Источники', sourceOpts, sel.sources)),
      h('div', { class: 'muted small', style: 'margin-left:auto' }, profiles.filter(p => p.enabled).length + ' ' + plural(profiles.filter(p => p.enabled).length, 'активный профиль', 'активных профиля', 'активных профилей'))),
    progressBox);

  append(root,
    h('div', { class: 'page-header' }, h('div', {}, h('h1', {}, 'Панель'), h('div', { class: 'sub' }, 'Поиск аккаунтов, состояние лотов и мониторинг исходников'))),
    h('div', { class: 'stack' }, hero, tilesBox,
      h('div', { class: 'grid grid-2' }, h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Последний поиск')), lastBox),
        h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Мониторинг исходников')), monitorBox)),
      h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Последние события'), h('a', { href: '#/log', class: 'small' }, 'Весь журнал →')), eventsBox)));

  // --- отрисовка динамических блоков ---
  const renderProgress = (st) => {
    const s = st.search || {};
    if (!s.running) { replace(progressBox); return; }
    const p = s.progress || {};
    const pct = p.total ? Math.min(100, Math.round((p.done || 0) / p.total * 100)) : null;
    replace(progressBox, h('div', { class: 'card progress-panel mt-16' },
      h('div', { class: 'row between mb-8' },
        h('div', { class: 'row' }, h('span', { class: 'spinner' }), h('b', {}, 'Идёт поиск: '), p.profile_name || p.profile_id || '…', p.source ? sourceChip(p.source) : null),
        h('button', { class: 'btn btn-danger btn-sm', onclick: (e) => busy(e.currentTarget, async () => { await API.post('/api/search/cancel'); toast('Поиск отменяется'); await tick(); }) }, 'Отмена')),
      h('div', { class: 'progress' + (pct == null ? ' indeterminate' : ''), role: 'progressbar', 'aria-valuenow': pct ?? 0 }, h('span', { style: pct == null ? '' : `width:${pct}%` })),
      h('div', { class: 'row between mt-8' },
        h('div', { class: 'progress-stats' },
          h('div', {}, h('div', { class: 'k' }, 'Получено'), h('div', { class: 'v' }, p.fetched ?? 0)),
          h('div', {}, h('div', { class: 'k' }, 'Подошло'), h('div', { class: 'v success' }, p.matched ?? 0)),
          h('div', {}, h('div', { class: 'k' }, 'Новых'), h('div', { class: 'v accent' }, p.new ?? 0))),
        h('div', { class: 'muted small' }, p.total ? `Шаг ${p.done || 0} из ${p.total}` : ''))));
  };
  const renderTiles = (st) => {
    const f = (st.counts && st.counts.found) || {}, l = (st.counts && st.counts.lots) || {};
    const today = extra.found.filter(x => isToday(x.first_seen)).length;
    const broken = extra.lots.filter(x => x.source_available === false).length;
    const tile = (label, value, sub, cls = '') => h('div', { class: 'stat-tile ' + cls }, h('div', { class: 'stat-label' }, label), h('div', { class: 'stat-value' }, nfRU.format(value || 0)), sub ? h('div', { class: 'stat-sub' }, sub) : null);
    replace(tilesBox,
      h('a', { href: '#/found', style: 'text-decoration:none;color:inherit' }, tile('Кандидатов', f.candidate || 0, `новых: ${f.new || 0} · отклонено: ${f.rejected || 0}`)),
      tile('Новых за сегодня', today, 'по времени первого обнаружения'),
      h('a', { href: '#/lots', style: 'text-decoration:none;color:inherit' }, tile('Активных лотов', l.active || 0, `черновиков: ${l.draft || 0} · снято: ${l.deactivated || 0}`)),
      tile('Исходник недоступен', broken, broken ? 'лоты требуют внимания' : 'все исходники на месте', broken ? 'alert' : ''));
  };
  const renderLast = (st) => {
    const last = (st.search && st.search.last) || [];
    if (!last.length) { replace(lastBox, emptyState('Поиск ещё не запускался', 'Нажмите «Найти аккаунты», чтобы начать')); return; }
    replace(lastBox, h('div', { class: 'table-wrap' }, h('table', { class: 'table compact' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Профиль'), h('th', {}, 'Источник'), h('th', { class: 'num' }, 'Получено'), h('th', { class: 'num' }, 'Подошло'), h('th', { class: 'num' }, 'Новых'), h('th', { class: 'num' }, 'Ошибки'))),
      h('tbody', {}, last.map(r => h('tr', {},
        h('td', {}, h('div', {}, profileName(r.profile_id)), h('div', { class: 'muted small', title: fmtDate(r.started_at) }, fmtRel(r.started_at) + (r.finished_at ? ' · ' + fmtDuration(r.started_at, r.finished_at) : ''))),
        h('td', {}, sourceChip(r.source)),
        h('td', { class: 'num' }, r.fetched), h('td', { class: 'num success' }, r.matched), h('td', { class: 'num accent' }, r.new),
        h('td', { class: 'num' }, (r.errors || []).length ? h('span', { class: 'chip danger', title: r.errors.join('\n') }, r.errors.length) : h('span', { class: 'muted' }, '—'))))))));
  };
  const renderMonitor = (st) => {
    const mo = st.monitor || {};
    replace(monitorBox,
      h('div', { class: 'grid grid-2 mb-16' },
        h('div', {}, h('div', { class: 'stat-label' }, 'Последняя проверка'), h('div', { class: 'bold', title: fmtDate(mo.last_run) }, mo.last_run ? fmtRel(mo.last_run) : 'ещё не было')),
        h('div', {}, h('div', { class: 'stat-label' }, 'Следующая проверка'), h('div', { class: 'bold', title: fmtDate(mo.next_run) }, mo.next_run ? fmtRel(mo.next_run) : (mo.interval_minutes ? 'не запланирована' : 'мониторинг выключен')))),
      h('div', { class: 'row between' },
        h('div', { class: 'muted small' }, mo.running ? h('span', { class: 'row' }, h('span', { class: 'spinner' }), 'идёт проверка…') : (mo.interval_minutes ? `интервал: ${mo.interval_minutes} мин` : 'интервал не задан')),
        h('button', { class: 'btn', disabled: !!mo.running, onclick: (e) => busy(e.currentTarget, async () => {
          const r = await API.post('/api/monitor/run');
          toast(`Проверено исходников: ${r.checked ?? 0}, недоступно: ${r.unavailable ?? 0}`, (r.unavailable || 0) > 0 ? 'warning' : 'success');
          await loadExtra(); await tick();
        }) }, 'Проверить сейчас')));
  };
  const renderEvents = () => {
    if (!extra.events.length) { replace(eventsBox, emptyState('Событий пока нет')); return; }
    replace(eventsBox, h('table', { class: 'table compact' }, h('tbody', {}, extra.events.slice(0, 10).map(ev => h('tr', { class: 'log-row' },
      h('td', { class: 'log-ts', title: fmtDate(ev.ts) }, fmtTime(ev.ts)), h('td', {}, h('span', { class: 'level ' + (ev.level || 'info') }, ev.level)),
      h('td', { class: 'muted small nowrap' }, ev.kind), h('td', {}, ev.message))))));
  };

  const loadExtra = async () => {
    const [found, lots, events] = await Promise.all([
      API.get('/api/found' + qs({ status: 'new,candidate', limit: 1000 })).catch(() => []),
      API.get('/api/lots').catch(() => []),
      API.get('/api/events' + qs({ limit: 10 })).catch(() => []),
    ]);
    extra = { found, lots, events };
    renderEvents();
  };
  const renderAll = (st) => { renderProgress(st); renderTiles(st); renderLast(st); renderMonitor(st); };

  /** Один цикл опроса: статус + (после завершения поиска) пересчёт данных. */
  const tick = async () => {
    const st = await loadStatus();
    const running = !!(st.search && st.search.running);
    if (wasRunning && !running) { toast('Поиск завершён', 'success'); await loadExtra(); }
    else if (running) { extra.events = await API.get('/api/events' + qs({ limit: 10 })).catch(() => extra.events); renderEvents(); }
    wasRunning = running;
    renderAll(st);
  };

  await loadExtra();
  renderAll(status);
  startPolling(tick, 2000);
}

// ----------------------------------------------------------------------------
// 6.2 Найдено
// ----------------------------------------------------------------------------

async function pageFound(root) {
  await loadProfiles();
  const filters = { profile_id: '', statuses: new Set(['candidate', 'new']), source: '', order: 'score', q: '' };
  const selected = new Set();
  let items = [];
  const listBox = h('div', { class: 'found-list' });
  const bulkBar = h('div', { class: 'bulk-bar' });
  const countEl = h('span', { class: 'muted small' });

  const load = async () => {
    replace(listBox, loadingState());
    try {
      items = await API.get('/api/found' + qs({ profile_id: filters.profile_id, status: [...filters.statuses].join(',') || null, source: filters.source, order: filters.order, limit: 200 }));
    } catch (e) { replace(listBox, emptyState('Ошибка загрузки', e.message, '⚠')); return; }
    for (const id of [...selected]) if (!items.some(x => x.id === id)) selected.delete(id);
    renderList();
  };
  const visible = () => {
    const q = filters.q.trim().toLowerCase();
    return q ? items.filter(f => [f.listing.title, f.listing.description, f.listing.seller_name, f.listing.region, f.profile_name].join(' ').toLowerCase().includes(q)) : items;
  };
  const renderBulk = () => {
    bulkBar.hidden = selected.size === 0;
    replace(bulkBar, h('b', {}, `Выбрано: ${selected.size}`),
      h('button', { class: 'btn btn-primary btn-sm', onclick: (e) => busy(e.currentTarget, async () => {
        let ok = 0, fail = 0;
        for (const id of [...selected]) { try { await API.post(`/api/found/${id}/create-lot`, { publish: false }); ok++; } catch (err) { fail++; } }
        toast(`Создано черновиков: ${ok}${fail ? ', ошибок: ' + fail : ''}`, fail ? 'warning' : 'success');
        selected.clear(); await load(); loadStatus().catch(() => {});
      }) }, 'Создать черновики для выбранных'),
      h('button', { class: 'btn btn-sm', onclick: (e) => busy(e.currentTarget, async () => {
        for (const id of [...selected]) { try { await API.post(`/api/found/${id}/status`, { status: 'ignored' }); } catch (err) { /* показываем итог ниже */ } }
        toast('Скрыто: ' + selected.size); selected.clear(); await load();
      }) }, 'Скрыть выбранные'),
      h('button', { class: 'btn btn-ghost btn-sm', onclick: () => { selected.clear(); renderList(); } }, 'Снять выделение'));
  };
  const renderList = () => {
    const list = visible();
    countEl.textContent = `${list.length} из ${items.length}`;
    renderBulk();
    replace(listBox, list.length ? list.map(f => foundCard(f, { selected, onSelect: renderBulk, reload: load, update: () => renderList() }))
      : emptyState('Ничего не найдено', items.length ? 'Измените текстовый фильтр' : 'Запустите поиск на панели или измените фильтры', '🔍'));
  };

  const statusOpts = Object.entries(FOUND_STATUS).map(([value, s]) => ({ value, label: s.label }));
  append(root,
    h('div', { class: 'page-header' }, h('div', {}, h('h1', {}, 'Найдено'), h('div', { class: 'sub' }, 'Объявления, подошедшие под критерии профилей')),
      h('div', { class: 'page-actions' }, countEl, h('button', { class: 'btn', onclick: load }, '⟳ Обновить'))),
    h('div', { class: 'filters' },
      h('select', { class: 'select', value: '', onchange: (e) => { filters.profile_id = e.target.value; load(); } }, h('option', { value: '' }, 'Все профили'), state.profiles.map(p => h('option', { value: p.id }, p.name))),
      h('select', { class: 'select', value: '', onchange: (e) => { filters.source = e.target.value; load(); } }, h('option', { value: '' }, 'Все источники'), h('option', { value: 'funpay' }, 'FunPay'), h('option', { value: 'lolz' }, 'Lolz')),
      h('select', { class: 'select', value: 'score', onchange: (e) => { filters.order = e.target.value; load(); } },
        h('option', { value: 'score' }, 'Сортировка: по баллам'), h('option', { value: 'price' }, 'Сортировка: по цене'), h('option', { value: 'first_seen' }, 'Сортировка: сначала новые'), h('option', { value: 'suggested_price' }, 'Сортировка: по нашей цене')),
      h('input', { class: 'input grow', type: 'search', placeholder: 'Поиск по названию, продавцу, региону…', oninput: debounce((e) => { filters.q = e.target.value; renderList(); }, 150) })),
    h('div', { class: 'filters' }, h('span', { class: 'label' }, 'Статус:'), chipToggles(statusOpts, filters.statuses, load)),
    bulkBar, listBox);
  bulkBar.hidden = true;
  await load();
}

/** Карточка найденного объявления. */
function foundCard(f, ctx) {
  const l = f.listing || {}, m = f.match || {};
  const checked = ctx.selected.has(f.id);
  const act = (label, cls, fn) => h('button', { class: 'btn btn-sm ' + cls, onclick: (e) => busy(e.currentTarget, fn) }, label);
  const actions = [
    act('Предпросмотр лота', 'btn-primary', () => openLotPreview(f, () => { ctx.reload(); loadStatus().catch(() => {}); })),
    act('Проверить', '', async () => {
      const r = await API.post(`/api/found/${f.id}/check`);
      f.available = r.available; f.last_checked = r.checked_at;
      toast(r.available === true ? 'Исходник доступен' : r.available === false ? 'Исходник недоступен' : 'Не удалось определить доступность', r.available === false ? 'warning' : 'info');
      ctx.update();
    }),
    f.status === 'ignored'
      ? act('Вернуть', '', async () => { await API.post(`/api/found/${f.id}/status`, { status: 'candidate' }); toast('Возвращено в кандидаты'); ctx.reload(); })
      : act('Скрыть', '', async () => { await API.post(`/api/found/${f.id}/status`, { status: 'ignored' }); toast('Объявление скрыто'); ctx.reload(); }),
    act('Удалить', 'btn-danger', async () => {
      if (!(await confirmDialog('Удалить запись о найденном объявлении? Оно может появиться снова при следующем поиске.', { ok: 'Удалить' }))) return;
      await API.del(`/api/found/${f.id}`); toast('Удалено'); ctx.reload(); loadStatus().catch(() => {});
    }),
  ];
  const card = h('div', { class: 'found-card' + (checked ? ' selected' : ''), dataset: { id: f.id } },
    h('label', { class: 'check' }, h('input', { type: 'checkbox', checked, 'aria-label': 'Выбрать', onchange: (e) => { e.target.checked ? ctx.selected.add(f.id) : ctx.selected.delete(f.id); card.classList.toggle('selected', e.target.checked); ctx.onSelect(); } })),
    h('div', { class: 'found-main' },
      h('div', { class: 'row' }, sourceChip(l.source), statusChip(f.status, FOUND_STATUS),
        l.region ? h('span', { class: 'chip soft' }, l.region) : null,
        f.profile_name ? h('span', { class: 'muted small' }, f.profile_name) : null,
        f.lot ? h('a', { href: '#/lots', class: 'chip ' + (LOT_STATUS[f.lot.status] || {}).cls, title: 'Наш лот #' + f.lot.id }, 'Лот: ' + ((LOT_STATUS[f.lot.status] || {}).label || f.lot.status)) : null),
      h('div', { class: 'found-title' }, h('a', { href: l.url, target: '_blank', rel: 'noopener', title: l.title }, l.title || '(без названия)')),
      h('div', { class: 'chip-row' }, h('span', { class: 'score', title: (m.reasons || []).join('\n') }, '★ ' + fmtNum(m.score)), (m.highlights || []).map(x => h('span', { class: 'chip hl' }, x))),
      h('div', { class: 'found-meta' },
        l.seller_name ? (l.seller_url ? h('a', { href: l.seller_url, target: '_blank', rel: 'noopener' }, '👤 ' + l.seller_name) : h('span', {}, '👤 ' + l.seller_name)) : null,
        availIndicator(f.available, f.last_checked),
        h('span', { title: 'Первое обнаружение: ' + fmtDate(f.first_seen) + '\nПоследнее: ' + fmtDate(f.last_seen) }, 'найдено ' + fmtRel(f.first_seen)),
        h('a', { href: l.url, target: '_blank', rel: 'noopener' }, 'Открыть источник ↗'))),
    h('div', { class: 'found-side' },
      h('div', { class: 'price-block' }, h('div', { class: 'price-src' }, 'Исходник: ' + fmtMoney(l.price, l.currency)),
        f.suggested_price != null ? h('div', { class: 'price-ours' }, h('span', { class: 'arrow' }, 'наша цена'), fmtMoney(f.suggested_price)) : null),
      h('div', { class: 'found-actions' }, actions)));
  return card;
}

// ----------------------------------------------------------------------------
// 6.3 Лоты
// ----------------------------------------------------------------------------

async function pageLots(root) {
  await loadProfiles();
  const filters = { statuses: new Set(['active', 'draft']) };
  const tableBox = h('div', { class: 'card' });
  const countEl = h('span', { class: 'muted small' });

  const load = async () => {
    replace(tableBox, loadingState());
    let lots;
    try { lots = await API.get('/api/lots' + qs({ status: [...filters.statuses].join(',') || null })); }
    catch (e) { replace(tableBox, emptyState('Ошибка загрузки', e.message, '⚠')); return; }
    countEl.textContent = lots.length + ' ' + plural(lots.length, 'лот', 'лота', 'лотов');
    if (!lots.length) { replace(tableBox, emptyState('Лотов нет', 'Создайте лот из найденного объявления на странице «Найдено»', '📦')); return; }
    replace(tableBox, h('div', { class: 'table-wrap' }, h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Статус'), h('th', {}, 'Лот'), h('th', { class: 'num' }, 'Цена'), h('th', { class: 'num' }, 'Маржа'), h('th', {}, 'Исходник'), h('th', { class: 'actions' }, 'Действия'))),
      h('tbody', {}, lots.map(lot => lotRow(lot, load))))));
  };
  const statusOpts = Object.entries(LOT_STATUS).map(([value, s]) => ({ value, label: s.label }));
  append(root,
    h('div', { class: 'page-header' }, h('div', {}, h('h1', {}, 'Лоты'), h('div', { class: 'sub' }, 'Наши лоты на FunPay, созданные по найденным объявлениям')),
      h('div', { class: 'page-actions' }, countEl, h('button', { class: 'btn', onclick: load }, '⟳ Обновить'))),
    h('div', { class: 'filters' }, h('span', { class: 'label' }, 'Статус:'), chipToggles(statusOpts, filters.statuses, load)),
    tableBox);
  await load();
}

function lotRow(lot, reload) {
  const margin = (lot.price || 0) - (lot.source_price || 0);
  const marginPct = lot.source_price ? margin / lot.source_price * 100 : null;
  const act = (label, cls, url, okMsg) => h('button', { class: 'btn btn-sm ' + cls, onclick: (e) => busy(e.currentTarget, async () => {
    const r = await API.post(`/api/lots/${lot.id}/${url}`);
    toast(r.status === 'error' ? `Ошибка: ${r.error || 'неизвестно'}` : okMsg, r.status === 'error' ? 'error' : 'success'); reload(); loadStatus().catch(() => {});
  }) }, label);
  const actions = [];
  if (lot.status === 'draft' || lot.status === 'error') actions.push(act('Опубликовать', 'btn-primary', 'publish', 'Лот опубликован'));
  if (lot.status === 'draft' || lot.status === 'error' || lot.status === 'deactivated') actions.push(h('button', { class: 'btn btn-sm', onclick: () => openLotEdit(lot, reload) }, 'Редактировать'));
  if (lot.status === 'active') { actions.push(act('Снять', '', 'deactivate', 'Лот снят с продажи')); actions.push(act('Проверить исходник', '', 'check', 'Исходник проверен')); }
  if (lot.status === 'deactivated') actions.push(act('Активировать', 'btn-success', 'activate', 'Лот активирован'));
  actions.push(h('button', { class: 'btn btn-sm btn-danger', onclick: (e) => busy(e.currentTarget, async () => {
    if (!(await confirmDialog(`Удалить лот #${lot.id}?${lot.status === 'active' ? ' Он активен на FunPay.' : ''}`, { ok: 'Удалить' }))) return;
    await API.del(`/api/lots/${lot.id}`); toast('Лот удалён'); reload(); loadStatus().catch(() => {});
  }) }, 'Удалить'));
  return h('tr', {},
    h('td', {}, statusChip(lot.status, LOT_STATUS)),
    h('td', { class: 'cell-title' },
      h('div', { class: 'bold ellipsis', title: lot.title_ru }, lot.title_ru || '(без заголовка)'),
      h('div', { class: 'muted small' }, '#' + lot.id, lot.profile_name ? ' · ' + lot.profile_name : '', ' · ', fmtRel(lot.updated_at)),
      h('div', { class: 'small links' }, lot.funpay_url ? [extLink(lot.funpay_url, 'на FunPay ↗'), ' · '] : null, extLink(lot.source_url, 'исходник ↗'), lot.seller_url ? [' · ', extLink(lot.seller_url, 'продавец ↗')] : null),
      lot.error ? h('div', { class: 'danger small' }, lot.error) : null),
    h('td', { class: 'num' }, h('div', { class: 'bold' }, fmtMoney(lot.price)), h('div', { class: 'small muted nowrap' }, 'из ' + fmtMoney(lot.source_price))),
    h('td', { class: 'num ' + (margin >= 0 ? 'success' : 'danger') }, fmtMoney(margin), h('div', { class: 'small muted' }, fmtPct(marginPct))),
    h('td', {}, availIndicator(lot.source_available, lot.source_checked_at)),
    h('td', { class: 'actions' }, h('div', { class: 'btn-group', style: 'justify-content:flex-end' }, actions)));
}

// ----------------------------------------------------------------------------
// 6.4 Профили (список)
// ----------------------------------------------------------------------------

async function pageProfiles(root) {
  const grid = h('div', { class: 'profile-grid' });
  const load = async () => {
    const [profiles, found] = await Promise.all([loadProfiles(), API.get('/api/found' + qs({ status: 'candidate,new', limit: 1000 })).catch(() => [])]);
    const counts = {};
    for (const f of found) counts[f.profile_id] = (counts[f.profile_id] || 0) + 1;
    replace(grid, profiles.length ? profiles.map(p => profileCard(p, counts[p.id] || 0, load))
      : h('div', { class: 'card', style: 'grid-column:1/-1' }, emptyState('Профилей пока нет', 'Профиль задаёт игру, критерии отбора, наценку и шаблон лота', '🎮')));
  };
  append(root,
    h('div', { class: 'page-header' }, h('div', {}, h('h1', {}, 'Профили'), h('div', { class: 'sub' }, 'Игра · регион · критерии · наценка · шаблон лота')),
      h('div', { class: 'page-actions' }, h('a', { class: 'btn btn-primary', href: '#/profiles/new' }, '+ Новый профиль'))),
    grid);
  await load();
}

function profileCard(p, candidates, reload) {
  const fp = (p.sources && p.sources.funpay) || {}, lz = (p.sources && p.sources.lolz) || {};
  return h('div', { class: 'card profile-card' + (p.enabled ? '' : ' disabled') },
    h('div', { class: 'row between' },
      h('div', { class: 'title' }, h('a', { href: '#/profiles/' + encodeURIComponent(p.id) }, p.name)),
      switchInput(p.enabled, null, async (v) => {
        try { await API.put(`/api/profiles/${encodeURIComponent(p.id)}`, { ...p, enabled: v }); toast(v ? 'Профиль включён' : 'Профиль выключен'); reload(); }
        catch (e) { toast(e.message, 'error'); reload(); }
      })),
    h('div', { class: 'chip-row' }, h('span', { class: 'chip soft' }, p.game), p.region ? h('span', { class: 'chip soft' }, p.region) : null,
      fp.enabled ? sourceChip('funpay') : null, lz.enabled ? sourceChip('lolz') : null, h('span', { class: 'chip neutral mono' }, p.id)),
    p.description ? h('div', { class: 'muted small' }, p.description) : null,
    h('div', { class: 'small muted' }, 'Наценка: ', pricingSummary(p.pricing), ' · Кандидатов: ', h('b', { class: candidates ? 'accent' : '' }, candidates)),
    h('div', { class: 'foot' },
      h('a', { class: 'btn btn-sm', href: '#/profiles/' + encodeURIComponent(p.id) }, 'Открыть'),
      h('div', { class: 'btn-group' },
        h('button', { class: 'btn btn-sm', onclick: (e) => busy(e.currentTarget, async () => { const np = await API.post(`/api/profiles/${encodeURIComponent(p.id)}/duplicate`); toast('Профиль скопирован: ' + np.id, 'success'); location.hash = '#/profiles/' + encodeURIComponent(np.id); }) }, 'Дублировать'),
        h('button', { class: 'btn btn-sm btn-danger', onclick: (e) => busy(e.currentTarget, async () => {
          if (!(await confirmDialog(`Удалить профиль «${p.name}»? Найденные объявления и лоты останутся.`, { ok: 'Удалить' }))) return;
          await API.del(`/api/profiles/${encodeURIComponent(p.id)}`); toast('Профиль удалён'); reload();
        }) }, 'Удалить'))));
}
function pricingSummary(pr) {
  if (!pr) return '—';
  switch (pr.mode) {
    case 'percent': return `+${fmtNum(pr.percent)}%`;
    case 'multiplier': return `×${fmtNum(pr.multiplier)}`;
    case 'formula': return pr.formula || 'формула';
    case 'tiers': return `ступени (${(pr.tiers || []).length})`;
    default: return pr.mode;
  }
}

// ----------------------------------------------------------------------------
// 6.5 Редактор профиля
// ----------------------------------------------------------------------------

function defaultProfile() {
  return {
    id: '', name: '', game: '', region: null, enabled: true, description: '',
    sources: {
      funpay: { enabled: true, subcategory_id: null, subcategory_ids: [], game_query: null, subcategory_query: 'Аккаунты', server_filter: [], extra_query: {}, max_items: 500 },
      lolz: { enabled: false, category: null, params: {}, pages: 3, max_items: 500 },
    },
    criteria: { price: { min: null, max: null }, must_any: [], must_all: [], exclude: [], regex_any: [], regex_exclude: [], numeric: [], highlights: [], regions: [], min_score: 1, seller_min_reviews: null },
    pricing: { mode: 'percent', percent: 90, multiplier: 1.9, formula: 'price * 2 - 1000', tiers: [], min_margin: 0, round_to: 100, round_mode: 'nearest', price_ending: null },
    lot_template: { funpay_subcategory_id: null, title_ru: '{game} | {highlights} | {region}', title_en: '', description_ru: '', description_en: '', amount: 1, deactivate_after_sale: true, active: true, fields: {} },
  };
}
/** Дополняет профиль с сервера значениями по умолчанию (чтобы все вложенные объекты существовали). */
function normalizeProfile(p) {
  const d = defaultProfile();
  const merge = (dst, src) => { for (const [k, v] of Object.entries(src || {})) { if (v && typeof v === 'object' && !Array.isArray(v) && dst[k] && typeof dst[k] === 'object' && !Array.isArray(dst[k])) merge(dst[k], v); else dst[k] = v; } return dst; };
  const out = merge(d, p);
  // у существующего профиля отсутствующий источник считается выключенным; у нового действуют значения по умолчанию
  if (p.sources && !p.sources.funpay) out.sources.funpay.enabled = false;
  if (p.sources && !p.sources.lolz) out.sources.lolz.enabled = false;
  return out;
}
const PLACEHOLDERS = ['{game}', '{region}', '{highlights}', '{highlights_lines}', '{price}', '{source_price}', '{source_title}', '{seller}', '{attr[key]}'];

async function pageProfileEditor(root, id) {
  const isNew = id === 'new';
  const model = normalizeProfile(isNew ? {} : await API.get(`/api/profiles/${encodeURIComponent(id)}`));
  if (!state.funpayCategories) API.get('/api/funpay/categories').then(c => { state.funpayCategories = c; }).catch(() => {});
  let tab = 'main';
  let lastText = null;   // последнее активное текстовое поле шаблона (для вставки плейсхолдеров)
  const tabBox = h('div');
  const titleEl = h('h1', {}, isNew ? 'Новый профиль' : model.name);

  const save = async () => {
    const pid = (model.id || '').trim();
    if (!/^[a-zA-Z0-9_-]+$/.test(pid)) throw new Error('Идентификатор: только латинские буквы, цифры, «_» и «-»');
    if (!model.name.trim()) throw new Error('Укажите название профиля');
    if (!model.game.trim()) throw new Error('Укажите игру');
    model.id = pid;
    const saved = isNew ? await API.post('/api/profiles', model) : await API.put(`/api/profiles/${encodeURIComponent(id)}`, model);
    toast('Профиль сохранён', 'success');
    if (isNew || saved.id !== id) location.hash = '#/profiles/' + encodeURIComponent(saved.id);
    else titleEl.textContent = saved.name;
  };

  const tabs = [['main', 'Основное'], ['sources', 'Источники'], ['criteria', 'Критерии'], ['pricing', 'Наценка'], ['template', 'Шаблон лота']];
  const tabsEl = h('div', { class: 'tabs', role: 'tablist' }, tabs.map(([key, label]) => h('button', { class: 'tab' + (key === tab ? ' active' : ''), role: 'tab', dataset: { tab: key }, onclick: () => { tab = key; renderTab(); } }, label)));
  const renderTab = () => {
    tabsEl.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    const render = { main: tabMain, sources: tabSources, criteria: tabCriteria, pricing: tabPricing, template: tabTemplate }[tab];
    replace(tabBox, render());
  };

  // --- Основное ---
  function tabMain() {
    return h('div', { class: 'card' }, h('div', { class: 'form-grid' },
      field('Идентификатор', textInput(model, 'id', { readonly: !isNew, placeholder: 'wot_eu', class: 'mono' }), isNew ? 'Латиница, цифры, «_» и «-». Имя файла профиля.' : 'Нельзя изменить после создания'),
      field('Название', textInput(model, 'name', { placeholder: 'World of Tanks EU' })),
      field('Игра', textInput(model, 'game', { placeholder: 'World of Tanks' }), 'Подставляется в шаблон как {game}'),
      field('Регион', textInput(model, 'region', { placeholder: 'EU', nullable: true }), 'Подставляется как {region}; пусто — любой'),
      field('Описание', textareaInput(model, 'description', { rows: 3, placeholder: 'Заметки для себя' }), null, 'span-2'),
      h('div', { class: 'span-2' }, checkInput(model, 'enabled', 'Профиль включён (участвует в поиске)'))));
  }

  // --- Источники ---
  function tabSources() {
    const fp = model.sources.funpay, lz = model.sources.lolz;
    const catLabel = h('div', { class: 'hint' }, funpayCategoryName(fp.subcategory_id) || (fp.subcategory_id ? 'Узел /lots/' + fp.subcategory_id + '/' : 'Категория не выбрана — будет поиск по названию игры'));
    const subIdInput = numberInput(fp, 'subcategory_id', { placeholder: 'например 1131', step: 1 });
    subIdInput.addEventListener('input', () => { catLabel.textContent = funpayCategoryName(fp.subcategory_id) || ''; });
    const lolzList = h('datalist', { id: 'lolz-cats' });
    const fillLolz = (cats) => replace(lolzList, cats.map(c => h('option', { value: c.name }, c.title)));
    if (state.lolzCategories) fillLolz(state.lolzCategories);
    else API.get('/api/lolz/categories').then(c => { state.lolzCategories = c; fillLolz(c); }).catch(() => {});
    return h('div', { class: 'stack' },
      h('div', { class: 'card' },
        h('div', { class: 'card-header' }, h('h2', {}, h('span', { class: 'row' }, sourceChip('funpay'), 'FunPay')), switchInput(fp.enabled, 'Искать на FunPay', (v) => { fp.enabled = v; })),
        h('div', { class: 'form-grid' },
          field('ID подкатегории (узел /lots/{id}/)', h('div', { class: 'input-group' }, subIdInput, h('button', { type: 'button', class: 'btn', onclick: () => pickFunPayCategory((c) => { fp.subcategory_id = c.id; subIdInput.value = c.id; catLabel.textContent = `${c.game} → ${c.name}`; }) }, 'Выбрать категорию')), catLabel),
          field('Дополнительные ID подкатегорий', tagInput(fp.subcategory_ids, { placeholder: 'ID и Enter', mono: true }), 'Если нужно искать сразу в нескольких узлах'),
          field('Игра по названию (если ID не задан)', textInput(fp, 'game_query', { placeholder: 'World of Tanks', nullable: true })),
          field('Раздел по названию', textInput(fp, 'subcategory_query', { placeholder: 'Аккаунты', nullable: true })),
          field('Фильтр по серверу/региону', tagInput(fp.server_filter, { placeholder: 'подстрока, напр. EU' }), 'Подстроки названия сервера; пусто — все'),
          field('Максимум объявлений', numberInput(fp, 'max_items', { step: 1, min: 1 })),
          field('Дополнительные GET-параметры фильтров', kvEditor(fp.extra_query, { keyPh: 'параметр', valPh: 'значение', addLabel: 'Добавить параметр' }), 'Например параметры фильтров страницы раздела', 'span-2'))),
      h('div', { class: 'card' },
        h('div', { class: 'card-header' }, h('h2', {}, h('span', { class: 'row' }, sourceChip('lolz'), 'Lolzteam Market')), switchInput(lz.enabled, 'Искать на Lolz', (v) => { lz.enabled = v; })),
        h('div', { class: 'form-grid' },
          field('Категория', h('div', { class: 'input-group' },
            h('input', { class: 'input', list: 'lolz-cats', placeholder: 'steam, world-of-tanks, fortnite…', value: lz.category ?? '', oninput: (e) => { lz.category = e.target.value || null; } }), lolzList,
            h('button', { type: 'button', class: 'btn', onclick: (e) => busy(e.currentTarget, async () => {
              if (!lz.category) throw new Error('Сначала укажите категорию');
              showJsonModal('Параметры категории ' + lz.category, await API.get('/api/lolz/params' + qs({ category: lz.category })));
            }) }, 'Параметры категории')), 'Можно выбрать из списка или ввести вручную'),
          field('Страниц', numberInput(lz, 'pages', { step: 1, min: 1 })),
          field('Максимум объявлений', numberInput(lz, 'max_items', { step: 1, min: 1 })),
          field('Параметры API', kvEditor(lz.params, { keyPh: 'pmin, game[] …', valPh: '1000 или [570]', addLabel: 'Добавить параметр',
            parse: (v) => { const t = v.trim(); if (/^(\[.*\]|\{.*\}|-?\d+(\.\d+)?|true|false|null)$/s.test(t)) { try { return JSON.parse(t); } catch (e) { /* строка */ } } return v; } }),
            'Числа и JSON-массивы распознаются: 1000, [570], true', 'span-2'))));
  }

  // --- Критерии ---
  function tabCriteria() {
    const c = model.criteria;
    const tester = { text: '', price: 10000 };
    const resultBox = h('div');
    const runTest = async () => {
      const r = await API.post('/api/matching/test', { profile: model, text: tester.text, price: toNum(tester.price) ?? 0 });
      replace(resultBox, h('div', { class: 'result-box ' + (r.matched ? 'ok' : 'bad') },
        h('div', { class: 'row mb-8' }, h('span', { class: 'chip ' + (r.matched ? 'success' : 'danger') }, r.matched ? 'Подходит' : 'Не подходит'), h('span', { class: 'score' }, 'баллы: ' + fmtNum(r.score)), (r.highlights || []).map(x => h('span', { class: 'chip hl' }, x))),
        (r.reasons || []).length ? [h('div', { class: 'label' }, 'Совпадения'), h('ul', { class: 'list-plain' }, r.reasons.map(x => h('li', {}, x)))] : null,
        (r.rejections || []).length ? [h('div', { class: 'label mt-8 danger' }, 'Причины отклонения'), h('ul', { class: 'list-plain' }, r.rejections.map(x => h('li', {}, x)))] : null));
    };
    return h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('div', { class: 'section-title' }, 'Цена и слова'),
        h('div', { class: 'form-grid' },
          field('Цена исходника, от', numberInput(c, 'price.min', { placeholder: 'без ограничения' })),
          field('Цена исходника, до', numberInput(c, 'price.max', { placeholder: 'без ограничения' })),
          field('Хотя бы одно из слов (must_any)', tagInput(c.must_any)),
          field('Все слова обязательны (must_all)', tagInput(c.must_all)),
          field('Стоп-слова (exclude)', tagInput(c.exclude)),
          field('«Фишки» для заголовка (highlights)', tagInput(c.highlights), 'Найденные слова попадут в {highlights}'),
          field('Регулярные выражения — хотя бы одно', tagInput(c.regex_any, { mono: true, placeholder: 'напр. \\b(\\d{2,3})\\s*танк' })),
          field('Регулярные выражения — исключить', tagInput(c.regex_exclude, { mono: true })),
          field('Допустимые регионы', tagInput(c.regions, { placeholder: 'RU, EU, NA…' }), 'Пусто — любой регион'))),
      h('div', { class: 'card' }, h('div', { class: 'section-title' }, 'Числовые правила и пороги'),
        h('div', { class: 'form-grid' },
          field('Правила по числовым полям (attributes)', rowsEditor(c.numeric, [{ key: 'field', label: 'поле', placeholder: 'tanks' }, { key: 'min', label: 'мин', type: 'number' }, { key: 'max', label: 'макс', type: 'number' }], { addLabel: 'Добавить правило', blank: () => ({ field: '' }) }), null, 'span-2'),
          field('Минимальный балл (min_score)', numberInput(c, 'min_score')),
          field('Минимум отзывов продавца', numberInput(c, 'seller_min_reviews', { step: 1, placeholder: 'не проверять' })))),
      h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Проверить критерии'), h('span', { class: 'muted small' }, 'Текст объявления прогоняется через текущие (несохранённые) критерии')),
        h('div', { class: 'form-grid' },
          field('Текст объявления', textareaInput(tester, 'text', { rows: 5, placeholder: 'Вставьте заголовок и описание объявления' }), null, 'span-2'),
          field('Цена', numberInput(tester, 'price')),
          h('div', { class: 'field' }, h('label', {}, ' '), h('button', { type: 'button', class: 'btn btn-primary', style: 'align-self:flex-start', onclick: (e) => busy(e.currentTarget, runTest) }, 'Проверить критерии'))),
        h('div', { class: 'mt-16' }, resultBox)));
  }

  // --- Наценка ---
  function tabPricing() {
    const pr = model.pricing;
    const modeBox = h('div', { class: 'span-2' });
    const preview = { price: 10000 };
    const previewBox = h('div', { class: 'result-box' }, h('span', { class: 'muted' }, 'Введите цену исходника'));
    const runPreview = debounce(async () => {
      const price = toNum(preview.price);
      if (price == null) return;
      try {
        const r = await API.post('/api/pricing/preview', { price, pricing: pr });
        const margin = r.price - price;
        replace(previewBox, h('div', { class: 'row' }, h('span', { class: 'muted' }, fmtMoney(price)), ' → ', h('b', { class: 'accent', style: 'font-size:18px' }, fmtMoney(r.price)),
          h('span', { class: 'muted small' }, `наценка ${fmtMoney(margin)} (${fmtPct(price ? margin / price * 100 : null)})`)));
      } catch (e) { replace(previewBox, h('span', { class: 'danger' }, e.message)); }
    }, 300);
    const renderMode = () => replace(modeBox, {
      percent: field('Наценка, %', numberInput(pr, 'percent'), 'цена × (1 + процент / 100)'),
      multiplier: field('Множитель', numberInput(pr, 'multiplier'), 'цена × множитель'),
      formula: field('Формула', textInput(pr, 'formula', { class: 'mono', placeholder: 'price * 2 - 1000' }), 'Переменная price, функции min/max/round/abs/floor/ceil, условия: price * 2 if price < 5000 else price * 1.5'),
      tiers: field('Ступени (до какой цены исходника — какой процент)', rowsEditor(pr.tiers, [{ key: 'up_to', label: 'цена до', type: 'number', placeholder: '10000' }, { key: 'percent', label: 'наценка, %', type: 'number', placeholder: '100' }], { addLabel: 'Добавить ступень', blank: () => ({}) }), 'Последняя ступень без «цена до» применяется ко всем остальным'),
    }[pr.mode] || null);
    renderMode();
    const card = h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('div', { class: 'form-grid' },
        field('Режим', selectInput(pr, 'mode', [{ value: 'percent', label: 'Процент наценки' }, { value: 'multiplier', label: 'Множитель' }, { value: 'formula', label: 'Формула' }, { value: 'tiers', label: 'Ступени' }], { onchange: () => { renderMode(); runPreview(); } })),
        field('Минимальная наценка, ₽', numberInput(pr, 'min_margin'), 'Итоговая цена не ниже исходной + эта сумма'),
        modeBox,
        field('Округление до', numberInput(pr, 'round_to', { step: 1, min: 0 }), '0 — без округления'),
        field('Способ округления', selectInput(pr, 'round_mode', [{ value: 'nearest', label: 'К ближайшему' }, { value: 'down', label: 'Вниз' }, { value: 'up', label: 'Вверх' }])),
        field('Окончание цены', numberInput(pr, 'price_ending', { step: 1, placeholder: 'например 990' }), 'Применяется после округления: 28 400 → 28 990'))),
      h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Предпросмотр цены')),
        h('div', { class: 'form-grid' }, field('Цена исходника, ₽', numberInput(preview, 'price')), field('Наша цена', previewBox))));
    card.addEventListener('input', runPreview);
    runPreview();
    return card;
  }

  // --- Шаблон лота ---
  function tabTemplate() {
    const t = model.lot_template;
    const catLabel = h('div', { class: 'hint' }, funpayCategoryName(t.funpay_subcategory_id) || 'Куда публикуем лот (узел /lots/{id}/)');
    const subIdInput = numberInput(t, 'funpay_subcategory_id', { placeholder: 'например 1131', step: 1 });
    const track = (el) => { el.addEventListener('focus', () => { lastText = el; }); return el; };
    const insert = (ph) => {
      const el = lastText || tabBox.querySelector('textarea');
      if (!el) return;
      const s = el.selectionStart ?? el.value.length, e = el.selectionEnd ?? s;
      el.value = el.value.slice(0, s) + ph + el.value.slice(e);
      el.selectionStart = el.selectionEnd = s + ph.length;
      el.dispatchEvent(new Event('input')); el.focus();
    };
    let fieldsEditor = kvEditor(t.fields, { keyPh: 'имя поля (напр. fields[server])', valPh: 'значение', addLabel: 'Добавить поле' });
    const loadForm = async () => {
      const sid = t.funpay_subcategory_id;
      if (!sid) throw new Error('Сначала укажите подкатегорию FunPay для лота');
      const schema = await API.get('/api/funpay/lot-form' + qs({ subcategory_id: sid }));
      const picks = {};
      const rows = schema.map(f => {
        const key = f.name;
        let control;
        if (f.type === 'select') control = h('select', { class: 'select sm', value: t.fields[key] ?? '', onchange: (e) => { picks[key] = e.target.value; } }, h('option', { value: '' }, '— не задавать —'), (f.options || []).map(o => h('option', { value: o.value }, o.label)));
        else if (f.type === 'checkbox') control = h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: !!t.fields[key], onchange: (e) => { picks[key] = e.target.checked ? (f.value || 'on') : ''; } }), 'включить');
        else if (f.type === 'hidden') control = h('span', { class: 'muted small mono' }, 'скрытое: ' + (f.value ?? ''));
        else control = h('input', { class: 'input sm', value: t.fields[key] ?? '', placeholder: f.value || '', oninput: (e) => { picks[key] = e.target.value; } });
        return h('tr', {}, h('td', {}, h('div', {}, f.label || key), h('div', { class: 'muted small mono' }, key, ' · ', f.type)), h('td', {}, control));
      });
      const m = openModal({ title: 'Поля формы лота FunPay', size: 'lg',
        body: schema.length ? h('table', { class: 'table compact' }, h('thead', {}, h('tr', {}, h('th', {}, 'Поле'), h('th', {}, 'Значение'))), h('tbody', {}, rows)) : emptyState('Форма не содержит дополнительных полей'),
        footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Отмена'), h('button', { class: 'btn btn-primary', onclick: () => {
          for (const [k, v] of Object.entries(picks)) { if (v === '' || v == null) delete t.fields[k]; else t.fields[k] = String(v); }
          fieldsEditor.refresh(); m.close(); toast('Поля применены к шаблону');
        } }, 'Применить выбранные')] });
    };
    return h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('div', { class: 'form-grid' },
        field('Подкатегория FunPay для публикации', h('div', { class: 'input-group' }, subIdInput, h('button', { type: 'button', class: 'btn', onclick: () => pickFunPayCategory((c) => { t.funpay_subcategory_id = c.id; subIdInput.value = c.id; catLabel.textContent = `${c.game} → ${c.name}`; }) }, 'Выбрать категорию')), catLabel),
        h('div', { class: 'field' }, h('label', {}, 'Плейсхолдеры (клик — вставить в поле)'), h('div', { class: 'cheatsheet' }, PLACEHOLDERS.map(p => h('code', { tabindex: 0, role: 'button', onclick: () => insert(p), onkeydown: (e) => { if (e.key === 'Enter') insert(p); } }, p)))),
        field('Заголовок (RU)', track(textInput(t, 'title_ru'))),
        field('Заголовок (EN)', track(textInput(t, 'title_en'))),
        field('Описание (RU)', track(textareaInput(t, 'description_ru', { rows: 7, placeholder: '{game} {region}\n\n{highlights_lines}\n\nБыстрая выдача, гарантия…' })), null, 'span-2'),
        field('Описание (EN)', track(textareaInput(t, 'description_en', { rows: 4 })), null, 'span-2'),
        field('Количество', numberInput(t, 'amount', { step: 1, min: 1 })),
        h('div', { class: 'field' }, h('label', {}, 'Поведение'), checkInput(t, 'active', 'Лот активен сразу после создания'), checkInput(t, 'deactivate_after_sale', 'Снимать лот после продажи')))),
      h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Дополнительные поля формы FunPay'), h('button', { type: 'button', class: 'btn', onclick: (e) => busy(e.currentTarget, loadForm) }, 'Загрузить поля формы FunPay')),
        h('div', { class: 'hint mb-8' }, 'Например fields[server] для выбора сервера. Загрузите схему формы, чтобы выбрать значения из списков.'),
        fieldsEditor));
  }

  append(root,
    h('div', { class: 'editor-bar' },
      h('div', { class: 'row' }, h('a', { class: 'btn btn-ghost btn-sm', href: '#/profiles' }, '← Профили'), titleEl, isNew ? null : h('span', { class: 'chip neutral mono' }, model.id)),
      h('div', { class: 'btn-group' }, h('a', { class: 'btn', href: '#/profiles' }, 'Отмена'), h('button', { class: 'btn btn-primary', onclick: (e) => busy(e.currentTarget, save) }, 'Сохранить'))),
    tabsEl, tabBox);
  renderTab();
}

// ----------------------------------------------------------------------------
// 6.6 Настройки
// ----------------------------------------------------------------------------

async function pageSettings(root) {
  const s = await API.get('/api/settings');
  const model = clone(s);
  const secrets = { golden_key: '', token: '' };
  const authBox = h('div');
  const form = h('div', { class: 'stack' },
    h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, h('span', { class: 'row' }, sourceChip('funpay'), 'FunPay')), s.funpay.golden_key_set ? h('span', { class: 'chip success' }, 'golden_key установлен') : h('span', { class: 'chip warning' }, 'golden_key не задан')),
      h('div', { class: 'form-grid' },
        field('golden_key (cookie)', h('input', { class: 'input mono', type: 'password', autocomplete: 'off', placeholder: s.funpay.golden_key_set ? 'оставьте пустым, чтобы не менять (' + s.funpay.golden_key + ')' : 'вставьте значение cookie', oninput: (e) => { secrets.golden_key = e.target.value; } }),
          'Войдите на funpay.com в браузере → DevTools (F12) → Application → Cookies → funpay.com → значение golden_key'),
        field('User-Agent', h('div', { class: 'input-group' }, textInput(model, 'funpay.user_agent', { placeholder: 'User-Agent того же браузера' }), h('button', { type: 'button', class: 'btn', onclick: (e) => { model.funpay.user_agent = navigator.userAgent; e.currentTarget.previousSibling.value = navigator.userAgent; } }, 'Вставить мой User-Agent')),
          'Должен совпадать с браузером, из которого взят golden_key'),
        field('Прокси', textInput(model, 'funpay.proxy', { placeholder: 'http://user:pass@host:port', nullable: true })),
        field('Пауза между запросами, с', numberInput(model, 'funpay.request_delay')),
        field('Таймаут, с', numberInput(model, 'funpay.timeout')),
        h('div', { class: 'span-2' }, checkInput(model, 'funpay.auto_publish', 'Публиковать лоты автоматически (без подтверждения)'),
          h('div', { class: 'callout warn mt-8' }, '⚠ При включении найденные кандидаты будут превращаться в активные лоты на FunPay без вашего участия. Рекомендуется оставить выключенным и создавать черновики вручную.')))),
    h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, h('span', { class: 'row' }, sourceChip('lolz'), 'Lolzteam Market')), s.lolz.token_set ? h('span', { class: 'chip success' }, 'токен установлен') : h('span', { class: 'chip warning' }, 'токен не задан')),
      h('div', { class: 'form-grid' },
        field('API-токен', h('input', { class: 'input mono', type: 'password', autocomplete: 'off', placeholder: s.lolz.token_set ? 'оставьте пустым, чтобы не менять (' + s.lolz.token + ')' : 'вставьте токен', oninput: (e) => { secrets.token = e.target.value; } }),
          h('span', {}, 'Получить: ', h('a', { href: 'https://lolz.team/account/api', target: '_blank', rel: 'noopener' }, 'lolz.team/account/api'), ' (права: market)')),
        field('Прокси', textInput(model, 'lolz.proxy', { placeholder: 'http://user:pass@host:port', nullable: true })),
        field('Пауза между запросами, с', numberInput(model, 'lolz.request_delay'), 'Лимит API — не чаще 1 запроса в 3 с'),
        field('Таймаут, с', numberInput(model, 'lolz.timeout')))),
    h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Мониторинг')),
      h('div', { class: 'form-grid' },
        h('div', { class: 'span-2 row', style: 'gap:24px' }, checkInput(model, 'monitor.enabled', 'Проверять доступность исходников'), checkInput(model, 'monitor.auto_deactivate', 'Снимать лот, если исходник продан')),
        field('Интервал проверки, мин', numberInput(model, 'monitor.interval_minutes', { step: 1, min: 1 })),
        field('Автопоиск каждые N минут', numberInput(model, 'monitor.auto_search_minutes', { step: 1, min: 0 }), '0 — автопоиск выключен'))),
    h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Интерфейс')),
      h('div', { class: 'form-grid-3' },
        field('Название', textInput(model, 'ui.brand')),
        field('Порт', numberInput(model, 'ui.port', { step: 1, min: 1 }), 'Применится после перезапуска'),
        h('div', { class: 'field' }, h('label', {}, ' '), checkInput(model, 'ui.open_browser', 'Открывать браузер при запуске')))),
    h('div', { class: 'card' }, h('div', { class: 'card-header' }, h('h2', {}, 'Проверка подключения'),
      h('button', { class: 'btn', onclick: (e) => busy(e.currentTarget, async () => {
        const r = await API.post('/api/auth/check');
        const row = (name, a) => h('div', { class: 'row' }, h('span', { class: 'dot ' + (a && a.ok ? 'ok' : 'bad') }), h('b', {}, name + ': '), a && a.ok ? h('span', { class: 'success' }, 'подключено' + (a.username ? ' как ' + a.username : '')) : h('span', { class: 'danger' }, (a && a.error) || 'ошибка'));
        replace(authBox, h('div', { class: 'result-box stack', style: 'gap:8px' }, row('FunPay', r.funpay), row('Lolz', r.lolz)));
        loadStatus().catch(() => {});
      }) }, 'Проверить подключение')), authBox));

  const save = async () => {
    const payload = clone(model);
    delete payload.funpay.golden_key_set; delete payload.lolz.token_set;
    if (secrets.golden_key.trim()) payload.funpay.golden_key = secrets.golden_key.trim(); else delete payload.funpay.golden_key;
    if (secrets.token.trim()) payload.lolz.token = secrets.token.trim(); else delete payload.lolz.token;
    await API.put('/api/settings', payload);
    toast('Настройки сохранены', 'success');
    navigate();
  };
  append(root,
    h('div', { class: 'page-header' }, h('div', {}, h('h1', {}, 'Настройки'), h('div', { class: 'sub' }, 'Доступы к площадкам, мониторинг и интерфейс')),
      h('div', { class: 'page-actions' }, h('button', { class: 'btn btn-primary', onclick: (e) => busy(e.currentTarget, save) }, 'Сохранить'))),
    form);
}

// ----------------------------------------------------------------------------
// 6.7 Журнал
// ----------------------------------------------------------------------------

async function pageLog(root) {
  const filters = { kind: '', levels: new Set() };
  let events = [];
  const kindSel = h('select', { class: 'select', onchange: (e) => { filters.kind = e.target.value; renderTable(); } }, h('option', { value: '' }, 'Все типы'));
  const tableBox = h('div', { class: 'card' });
  const countEl = h('span', { class: 'muted small' });
  const levelOpts = [['debug', 'debug'], ['info', 'info'], ['success', 'success'], ['warning', 'warning'], ['error', 'error']].map(([value, label]) => ({ value, label }));

  const renderTable = () => {
    const known = new Set([...kindSel.options].map(o => o.value));
    for (const k of new Set(events.map(e => e.kind).filter(Boolean))) if (!known.has(k)) kindSel.append(h('option', { value: k }, k));
    const list = events.filter(e => (!filters.kind || e.kind === filters.kind) && (!filters.levels.size || filters.levels.has(e.level)));
    countEl.textContent = `${list.length} из ${events.length}`;
    if (!list.length) { replace(tableBox, emptyState('Событий нет')); return; }
    replace(tableBox, h('div', { class: 'table-wrap' }, h('table', { class: 'table compact' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Время'), h('th', {}, 'Уровень'), h('th', {}, 'Тип'), h('th', {}, 'Сообщение'))),
      h('tbody', {}, list.map(ev => h('tr', { class: 'log-row' },
        h('td', { class: 'log-ts', title: fmtDate(ev.ts) }, fmtDate(ev.ts)),
        h('td', {}, h('span', { class: 'level ' + (ev.level || 'info') }, ev.level || 'info')),
        h('td', { class: 'muted nowrap' }, ev.kind),
        h('td', {}, ev.message, ev.data && Object.keys(ev.data).length ? h('details', { class: 'log-data' }, h('summary', {}, 'данные'), h('pre', {}, JSON.stringify(ev.data, null, 2))) : null)))))));
  };
  const load = async () => { events = await API.get('/api/events' + qs({ limit: 200 })); renderTable(); };
  append(root,
    h('div', { class: 'page-header' }, h('div', {}, h('h1', {}, 'Журнал'), h('div', { class: 'sub' }, 'События приложения, обновляется каждые 5 секунд')),
      h('div', { class: 'page-actions' }, countEl, h('button', { class: 'btn', onclick: (e) => busy(e.currentTarget, load) }, '⟳ Обновить'))),
    h('div', { class: 'filters' }, kindSel, h('span', { class: 'label' }, 'Уровень:'), chipToggles(levelOpts, filters.levels, renderTable)),
    tableBox);
  await load();
  startPolling(load, 5000);
}

// ----------------------------------------------------------------------------
// Запуск
// ----------------------------------------------------------------------------

window.addEventListener('hashchange', navigate);
window.addEventListener('DOMContentLoaded', navigate);
// Закрываем открытые выпадающие списки (<details class="dropdown">) при клике вне их
document.addEventListener('click', (e) => {
  document.querySelectorAll('details.dropdown[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; });
});
