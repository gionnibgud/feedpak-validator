// SPDX-License-Identifier: AGPL-3.0-only
// feedback-validator — validates .feedpak packages in-app and exposes a
// window.feedBackValidator service other plugins (e.g. the editor) can call.
(function () {
    'use strict';

    const BASE = '/api/plugins/feedback-validator';

    // ── HTTP service (DOM-independent, available at load) ──────────────────
    async function _post(path, opts) {
        const r = await fetch(BASE + path, opts);
        if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
        return r.json();
    }

    function validateIds(ids, strict) {
        return _post('/validate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids, strict: !!strict }),
        });
    }

    function validateFiles(files, strict) {
        const fd = new FormData();
        for (const f of files) fd.append('files', f, f.name);
        fd.append('strict', strict ? 'true' : 'false');
        return _post('/validate-upload', { method: 'POST', body: fd });
    }

    // Service other plugins call. Each returns a single result dict:
    //   { pack, level, ok, errors:[str], warnings:[str] }
    // Defaults to strict — basic is spec-conformance only and misses invariants
    // like notation measures overflowing their time signature; callers that
    // want the looser check opt in with { strict: false }.
    window.feedBackValidator = {
        async validatePack(id, { strict = true } = {}) {
            return (await validateIds([id], strict)).results[0];
        },
        async validateBytes(blob, { strict = true, filename = 'song.feedpak' } = {}) {
            const file = blob instanceof File ? blob : new File([blob], filename);
            return (await validateFiles([file], strict)).results[0];
        },
        validate(input, opts = {}) {
            return typeof input === 'string'
                ? window.feedBackValidator.validatePack(input, opts)
                : window.feedBackValidator.validateBytes(input, opts);
        },
    };
    // Let late-loading consumers wait for us instead of racing plugin load order.
    window.feedBack?.emit?.('validator:ready');

    // ── Standalone UI ──────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);

    // A library can hold thousands of packs. /packs is paginated server-side
    // (see routes.py _DEFAULT_PACK_LIMIT), so the list shown here is always a
    // bounded page — search narrows it. Selection is tracked independently of
    // what's currently rendered so picking packs across multiple searches
    // doesn't lose earlier picks when the DOM list is replaced.
    const PAGE_LIMIT = 300;
    const MAX_BATCH = 200;   // must match routes.py _MAX_VALIDATE_BATCH
    const _selected = new Set();

    // Friendly errors read "<file>: <where>: <cause>". Peel a leading path-like
    // segment into its own dim tag; leave the rest (which already reads well) as
    // the message. Falls back to the raw string when there's no path prefix.
    function splitError(s) {
        const i = s.indexOf(': ');
        const head = i === -1 ? '' : s.slice(0, i);
        if (i !== -1 && /[/.]/.test(head) && !head.includes(' ')) {
            return { file: head, msg: s.slice(i + 2) };
        }
        return { file: '', msg: s };
    }

    function lineEl(kind, text, explanation) {
        const li = document.createElement('li');
        li.className = 'fbv-line fbv-' + kind;

        const row = document.createElement('div');
        row.className = 'fbv-line-tech';
        const { file, msg } = splitError(text);
        if (file) {
            const tag = document.createElement('code');
            tag.className = 'fbv-file';
            tag.textContent = file;
            row.appendChild(tag);
        }
        const span = document.createElement('span');
        span.textContent = msg;
        row.appendChild(span);
        li.appendChild(row);

        if (explanation) {
            const ex = document.createElement('div');
            ex.className = 'fbv-line-explain';
            ex.textContent = explanation;
            li.appendChild(ex);
        }
        return li;
    }

    // A plain-language takeaway for someone who doesn't know what a JSON
    // Schema is — sits always-visible under the PASS/FAIL line, above the
    // collapsible technical detail. Deliberately doesn't parse error text
    // (fragile, and drifts as strict grows); it only reasons about counts
    // and level, so it stays accurate as new checks are added.
    function humanSummary(res) {
        const nErr = res.errors?.length || 0;
        const nWarn = res.warnings?.length || 0;
        if (res.ok && !nWarn) {
            return res.level === 'strict'
                ? 'This pack is fully valid — everything checks out.'
                : "This pack meets the basic requirements, but hasn't been checked as "
                  + 'thoroughly as it could be. Turn on Strict above for a more complete check.';
        }
        if (res.ok) {
            return `This pack is valid, but has ${nWarn} thing${nWarn === 1 ? '' : 's'} `
                 + 'worth a look (see below).';
        }
        return `This pack has ${nErr} problem${nErr === 1 ? '' : 's'} that need fixing before `
             + 'it will work correctly in feedBack — see below for exactly what and where.';
    }

    function card(res) {
        const el = document.createElement('div');
        el.className = 'fbv-card ' + (res.ok ? 'fbv-ok' : 'fbv-fail');

        const sum = document.createElement('div');
        sum.className = 'fbv-card-sum';
        const badge = document.createElement('span');
        badge.className = 'fbv-badge';
        badge.textContent = res.ok ? 'PASS' : 'FAIL';
        const name = document.createElement('span');
        name.className = 'fbv-card-name';
        name.textContent = res.pack;
        const lvl = document.createElement('span');
        lvl.className = 'fbv-card-lvl';
        lvl.textContent = res.level;
        sum.append(badge, name, lvl);
        el.appendChild(sum);

        const human = document.createElement('p');
        human.className = 'fbv-card-human';
        human.textContent = humanSummary(res);
        el.appendChild(human);

        const nErr = res.errors?.length || 0;
        const nWarn = res.warnings?.length || 0;
        if (nErr || nWarn) {
            const details = document.createElement('details');
            details.className = 'fbv-card-details';
            details.open = !res.ok || nWarn > 0;
            const detSum = document.createElement('summary');
            detSum.textContent = 'Technical details';
            details.appendChild(detSum);
            if (nErr) {
                const ul = document.createElement('ul');
                ul.className = 'fbv-lines';
                res.errors.forEach((e, i) => ul.appendChild(lineEl('err', e, res.explanations?.[i])));
                details.appendChild(ul);
            }
            if (nWarn) {
                const ul = document.createElement('ul');
                ul.className = 'fbv-lines';
                res.warnings.forEach((w, i) => ul.appendChild(lineEl('warn', 'warning: ' + w, res.warning_explanations?.[i])));
                details.appendChild(ul);
            }
            el.appendChild(details);
        }
        return el;
    }

    function render(resp) {
        const out = $('fbv-results');
        if (!out) return;
        out.textContent = '';
        const { results = [], passed = 0, total = 0 } = resp || {};
        if (!total) { out.textContent = 'Nothing selected to validate.'; return; }
        const head = document.createElement('div');
        head.className = 'fbv-summary ' + (passed === total ? 'fbv-ok' : 'fbv-fail');
        head.textContent = `${passed}/${total} valid`;
        out.appendChild(head);
        results.forEach((r) => out.appendChild(card(r)));
    }

    function updateSelectedCount() {
        const el = $('fbv-selcount');
        if (el) el.textContent = _selected.size ? `${_selected.size} selected` : '';
        const btn = $('fbv-validate');
        if (btn) {
            btn.title = _selected.size > MAX_BATCH
                ? `Select ${MAX_BATCH} or fewer at a time (currently ${_selected.size})` : '';
        }
    }

    function renderPacks(items, total, query) {
        const box = $('fbv-packs');
        const note = $('fbv-packs-note');
        if (!box) return;
        box.textContent = '';
        if (!items.length) {
            box.textContent = query ? `No packs match "${query}".` : 'No packs found in the library.';
        }
        for (const p of items) {
            const row = document.createElement('label');
            row.className = 'fbv-pack';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = p.id;
            cb.checked = _selected.has(p.id);
            cb.addEventListener('change', () => {
                if (cb.checked) _selected.add(p.id); else _selected.delete(p.id);
                updateSelectedCount();
            });
            const nm = document.createElement('span');
            nm.textContent = p.name;
            row.append(cb, nm);
            box.appendChild(row);
        }
        if (note) {
            note.textContent = total > items.length
                ? `Showing ${items.length} of ${total} — refine your search to narrow down.`
                : (total ? `${total} pack${total === 1 ? '' : 's'}` : '');
        }
        updateSelectedCount();
    }

    async function loadSpecInfo() {
        const el = $('fbv-spec-version');
        if (!el) return;
        try {
            const info = await _post('/spec-info', {});
            if (!info.tag) { return; }   // VENDOR.txt missing/unparsable — say nothing rather than guess
            el.textContent = `Reference: feedpak-spec ${info.tag}`;
            if (info.repo && info.commit) {
                const a = document.createElement('a');
                a.href = `${info.repo}/tree/${info.commit}`;
                a.target = '_blank';
                a.rel = 'noopener noreferrer';
                a.textContent = ` (${info.commit.slice(0, 7)})`;
                el.appendChild(a);
            }
        } catch (e) { /* purely informational — a failed fetch just leaves the line blank */ }
    }

    async function loadPacks(query = '') {
        const box = $('fbv-packs');
        if (!box) return;
        box.textContent = 'Loading…';
        try {
            const params = new URLSearchParams({ q: query, limit: String(PAGE_LIMIT) });
            const r = await fetch(`${BASE}/packs?${params}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const { items, total } = await r.json();
            renderPacks(items, total, query);
        } catch (e) {
            box.textContent = 'Failed to load packs: ' + e.message;
        }
    }

    async function runSelected() {
        const strict = $('fbv-strict')?.checked;
        const ids = [..._selected];
        if (!ids.length) { render({ results: [], passed: 0, total: 0 }); return; }
        if (ids.length > MAX_BATCH) {
            $('fbv-results').textContent =
                `Select ${MAX_BATCH} or fewer packs at a time — you have ${ids.length} selected. ` +
                `Narrow your search or validate in smaller batches.`;
            return;
        }
        $('fbv-validate').disabled = true;
        try { render(await validateIds(ids, strict)); }
        catch (e) { $('fbv-results').textContent = 'Validation failed: ' + e.message; }
        finally { $('fbv-validate').disabled = false; }
    }

    async function runUpload(files) {
        if (!files || !files.length) return;
        const strict = $('fbv-strict')?.checked;
        try { render(await validateFiles(files, strict)); }
        catch (e) { $('fbv-results').textContent = 'Validation failed: ' + e.message; }
    }

    function wire() {
        const btn = $('fbv-validate');
        if (!btn || btn._fbvWired) return;   // idempotent — screen:changed re-fires
        btn._fbvWired = true;

        btn.addEventListener('click', runSelected);

        const search = $('fbv-search');
        $('fbv-refresh')?.addEventListener('click', () => loadPacks(search?.value.trim() || ''));
        let _searchTimer = null;
        search?.addEventListener('input', () => {
            clearTimeout(_searchTimer);
            _searchTimer = setTimeout(() => loadPacks(search.value.trim()), 250);
        });
        $('fbv-selall')?.addEventListener('click', () => {
            document.querySelectorAll('#fbv-packs input[type=checkbox]').forEach((c) => {
                c.checked = true;
                _selected.add(c.value);
            });
            updateSelectedCount();
        });
        $('fbv-clearsel')?.addEventListener('click', () => {
            _selected.clear();
            document.querySelectorAll('#fbv-packs input[type=checkbox]').forEach((c) => { c.checked = false; });
            updateSelectedCount();
        });

        const drop = $('fbv-drop');
        const file = $('fbv-file');
        drop?.addEventListener('click', () => file?.click());
        drop?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file?.click(); }
        });
        file?.addEventListener('change', () => runUpload(file.files));
        drop?.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('fbv-drag'); });
        drop?.addEventListener('dragleave', () => drop.classList.remove('fbv-drag'));
        drop?.addEventListener('drop', (e) => {
            e.preventDefault();
            drop.classList.remove('fbv-drag');
            runUpload(e.dataTransfer.files);
        });

        loadSpecInfo();
        loadPacks('');
    }

    // The screen fragment is mounted at plugin load; wire when it's present and
    // again whenever the user navigates to it.
    if (document.readyState !== 'loading') wire();
    else document.addEventListener('DOMContentLoaded', wire);
    window.feedBack?.on?.('screen:changed', wire);
})();
