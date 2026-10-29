(function () {
  const grid = document.getElementById('explore-grid');
  const filters = document.getElementById('explore-filters');
  const moreBtn = document.getElementById('load-more');
  const errEl = document.getElementById('explore-error');
  const loadingEl = document.getElementById('explore-loading');

  // TikTok's explore feed rotates a fresh batch on every request rather than
  // paginating, so we de-duplicate by video id as batches come in.
  const seen = new Set();
  let category = window.EXPLORE_CONFIG.defaultCategory;
  let busy = false;

  function setError(msg) {
    if (msg) {
      errEl.textContent = msg;
      errEl.classList.remove('hidden');
    } else {
      errEl.classList.add('hidden');
    }
  }

  function card(item) {
    const a = document.createElement('a');
    a.className = 'explore-card';
    a.href = item.url;

    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = '';
    if (item.thumb) {
      img.src = item.thumb;
    }
    a.appendChild(img);

    const meta = document.createElement('div');
    meta.className = 'explore-card-meta';

    const author = document.createElement('span');
    author.className = 'explore-card-author';
    author.textContent = '@' + item.author;
    meta.appendChild(author);

    const desc = document.createElement('span');
    desc.className = 'explore-card-desc';
    desc.textContent = item.desc || '';
    meta.appendChild(desc);

    a.appendChild(meta);
    return a;
  }

  async function loadMore() {
    if (busy) return;
    busy = true;
    moreBtn.disabled = true;
    moreBtn.textContent = 'Loading…';
    if (!grid.children.length) loadingEl.classList.remove('hidden');
    setError('');

    try {
      const resp = await fetch('/api/explore?category=' + encodeURIComponent(category));
      if (!resp.ok) {
        let msg = 'Could not load Explore.';
        try {
          const body = await resp.json();
          if (body && typeof body.detail === 'string') msg = body.detail;
        } catch (_) {}
        throw new Error(msg);
      }
      const data = await resp.json();
      for (const item of data.items || []) {
        if (seen.has(item.id)) continue;
        seen.add(item.id);
        grid.appendChild(card(item));
      }
      if (!grid.children.length) {
        setError('No videos returned right now.');
      }
    } catch (e) {
      setError(e.message || 'Could not load Explore.');
    } finally {
      busy = false;
      moreBtn.disabled = false;
      moreBtn.textContent = 'Load more';
      loadingEl.classList.add('hidden');
    }
  }

  filters.addEventListener('click', function (e) {
    const btn = e.target.closest('.explore-filter');
    if (!btn || busy) return;
    category = Number(btn.dataset.category);
    seen.clear();
    grid.innerHTML = '';
    setError('');
    filters.querySelectorAll('.explore-filter').forEach(function (b) {
      b.classList.toggle('active', b === btn);
    });
    loadMore();
  });

  moreBtn.addEventListener('click', loadMore);

  loadMore();
})();
