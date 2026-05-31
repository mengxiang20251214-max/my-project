/**
 * VideoHub Pro v3 — 主 JavaScript
 * • initBannerSwiper(): Swiper 轮播初始化
 * • 工具函数：Toast、外链安全、图片容错
 */

// ── Swiper Banner 初始化 ────────────────────────────────────────────────────────

/**
 * 初始化单个 Banner Swiper
 * @param {string}  selector   CSS 选择器
 * @param {object}  opts
 *   duration   {number}  自动播放间隔(ms)
 *   pagination {string}  分页点 CSS 选择器（可选）
 *   navigation {boolean} 是否显示前进后退按钮
 *   effect     {string}  切换效果，默认 'fade'
 */
function initBannerSwiper(selector, opts = {}) {
  const el = document.querySelector(selector);
  if (!el) return null;

  const slides = el.querySelectorAll('.swiper-slide');
  if (slides.length === 0) return null;

  const cfg = {
    loop:    slides.length > 1,
    autoplay: slides.length > 1 ? {
      delay:                 opts.duration || 3000,
      disableOnInteraction:  false,
      pauseOnMouseEnter:     true,
    } : false,
    effect:      opts.effect || 'fade',
    fadeEffect:  { crossFade: true },
    speed:       600,
    grabCursor:  true,
  };

  if (opts.pagination) {
    cfg.pagination = { el: opts.pagination, clickable: true };
  }
  if (opts.navigation) {
    cfg.navigation = {
      nextEl: `${selector} .swiper-button-next`,
      prevEl: `${selector} .swiper-button-prev`,
    };
  }

  try {
    return new Swiper(selector, cfg);
  } catch (e) {
    console.warn('[Banner] Swiper init failed for', selector, e.message);
    return null;
  }
}

// ── DOM 就绪初始化 ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  fixExternalLinks();
  fixBrokenImages();
});

/** 给所有 target="_blank" 链接加安全属性 */
function fixExternalLinks() {
  document.querySelectorAll('a[target="_blank"]').forEach(a => {
    if (!a.rel.includes('noopener')) a.rel += ' noopener noreferrer';
  });
}

/** 图片加载失败时隐藏，防止 broken icon */
function fixBrokenImages() {
  document.querySelectorAll('img').forEach(img => {
    img.addEventListener('error', function() { this.style.visibility = 'hidden'; });
  });
}

// ── 轻提示 Toast ────────────────────────────────────────────────────────────────
function showToast(msg, type = 'info', duration = 3000) {
  const colors = { success: '#10b981', error: '#ef4444', info: '#6366f1' };
  const el = document.createElement('div');
  Object.assign(el.style, {
    position:   'fixed', top:'76px', right:'16px', zIndex:9999,
    padding:    '0.75rem 1rem', borderRadius:'0.75rem',
    color:      '#fff', fontSize:'0.875rem',
    background: colors[type] || colors.info,
    boxShadow:  '0 4px 12px rgba(0,0,0,0.15)',
    transition: 'opacity .3s, transform .3s',
  });
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transform = 'translateX(12px)';
    setTimeout(() => el.remove(), 300);
  }, duration);
}

// ── 工具 ────────────────────────────────────────────────────────────────────────
function copyLink() {
  navigator.clipboard.writeText(location.href)
    .then(() => showToast('链接已复制', 'success'))
    .catch(() => showToast('复制失败', 'error'));
}

function fmtBytes(n) {
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
  return (n/1073741824).toFixed(2) + ' GB';
}
