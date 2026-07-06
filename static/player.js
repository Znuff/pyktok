(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Video controls — show native controls on hover / touch
  // ---------------------------------------------------------------------------
  const wrapper = document.querySelector('.video-wrapper');
  const video = document.getElementById('main-video');
  let touchTimer = null;

  // ---------------------------------------------------------------------------
  // Volume persistence via localStorage
  // ---------------------------------------------------------------------------
  const VOLUME_KEY = 'pyktok_volume';
  const MUTED_KEY  = 'pyktok_muted';

  function loadVolume() {
    const savedVolume = localStorage.getItem(VOLUME_KEY);
    const savedMuted  = localStorage.getItem(MUTED_KEY);
    if (savedVolume !== null) video.volume = parseFloat(savedVolume);
    if (savedMuted  !== null) video.muted  = savedMuted === 'true';
  }

  function saveVolume() {
    localStorage.setItem(VOLUME_KEY, video.volume);
    localStorage.setItem(MUTED_KEY,  video.muted);
  }

  if (wrapper && video) {
    // Apply saved volume before first play
    loadVolume();

    // Attempt autoplay. Browsers with sufficient Media Engagement Index (MEI)
    // for this domain will allow unmuted autoplay. If blocked, show play button.
    const playBtn = document.getElementById('play-btn');

    video.play().catch(() => {
      playBtn && playBtn.classList.remove('hidden');
    });

    if (playBtn) {
      playBtn.addEventListener('click', () => {
        video.play().then(() => playBtn.classList.add('hidden'));
      });
    }

    // Hide play button once video actually starts
    video.addEventListener('play', () => {
      playBtn && playBtn.classList.add('hidden');
    });

    // Persist whenever the user adjusts volume or toggles mute via native controls
    video.addEventListener('volumechange', saveVolume);

    wrapper.addEventListener('mouseenter', () => video.setAttribute('controls', ''));
    wrapper.addEventListener('mouseleave', () => {
      // Don't remove controls if options panel is open
      if (!document.getElementById('options-panel')?.classList.contains('open')) {
        video.removeAttribute('controls');
      }
    });

    // Touch: show controls for 3 s then hide
    wrapper.addEventListener('touchstart', () => {
      video.setAttribute('controls', '');
      clearTimeout(touchTimer);
      touchTimer = setTimeout(() => video.removeAttribute('controls'), 3000);
    }, { passive: true });

    // Unmute on click if no saved preference exists yet
    video.addEventListener('click', () => {
      if (video.muted && localStorage.getItem(MUTED_KEY) === null) {
        video.muted = false;
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Options panel toggle
  // ---------------------------------------------------------------------------
  const optionsBtn = document.getElementById('options-btn');
  const optionsPanel = document.getElementById('options-panel');

  if (optionsBtn && optionsPanel) {
    optionsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      optionsPanel.classList.toggle('open');
    });

    // Close panel when clicking outside
    document.addEventListener('click', (e) => {
      if (optionsPanel.classList.contains('open') &&
          !optionsPanel.contains(e.target) &&
          e.target !== optionsBtn) {
        optionsPanel.classList.remove('open');
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Description badge toggle (click author handle)
  // ---------------------------------------------------------------------------
  const authorHandle = document.getElementById('author-handle');
  const descBadge = document.getElementById('desc-badge');

  if (authorHandle && descBadge) {
    authorHandle.addEventListener('click', () => {
      descBadge.classList.toggle('visible');
    });

    // Also hide badge on click elsewhere
    document.addEventListener('click', (e) => {
      if (e.target !== authorHandle && descBadge.classList.contains('visible')) {
        descBadge.classList.remove('visible');
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Share button — copy current URL to clipboard
  // ---------------------------------------------------------------------------
  const btnShare = document.getElementById('btn-share');

  if (btnShare) {
    btnShare.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(window.location.href);
        const orig = btnShare.textContent;
        btnShare.textContent = 'Copied!';
        setTimeout(() => { btnShare.textContent = orig; }, 1500);
      } catch (_) {
        // Fallback for browsers without clipboard API
        prompt('Copy this link:', window.location.href);
      }
    });
  }

})();
