/**
 * Global Floating Tooltip Manager
 * Binds dynamically to any [data-tooltip] elements and renders a fixed floating bubble,
 * avoiding any overflow/clipping from parent containers (modals, tables, cards, etc.).
 */

let tooltipEl = null;
let activeTarget = null;

function initGlobalTooltip() {
    if (!tooltipEl) {
        tooltipEl = document.createElement('div');
        tooltipEl.className = 'floating-tooltip';
        tooltipEl.id = 'global-floating-tooltip';
        document.body.appendChild(tooltipEl);
    }

    // Event delegation on document for mouseover
    document.addEventListener('mouseover', (e) => {
        const target = e.target.closest('[data-tooltip]');
        if (!target) return;
        const text = target.getAttribute('data-tooltip');
        if (!text || !text.trim()) return;

        showTooltip(target, text.trim());
    });

    // Event delegation on document for mouseout
    document.addEventListener('mouseout', (e) => {
        if (!activeTarget) return;
        const related = e.relatedTarget;
        if (!related || !activeTarget.contains(related)) {
            hideTooltip();
        }
    });

    // Hide on scroll or resize to prevent floating detached
    window.addEventListener('scroll', () => {
        if (activeTarget) hideTooltip();
    }, { passive: true });

    document.addEventListener('scroll', (e) => {
        if (activeTarget) hideTooltip();
    }, { passive: true, capture: true });

    window.addEventListener('resize', () => {
        if (activeTarget) hideTooltip();
    }, { passive: true });
}

function showTooltip(target, text) {
    if (!tooltipEl) return;
    activeTarget = target;
    tooltipEl.textContent = text;
    tooltipEl.classList.remove('visible');

    // Make visible in DOM to measure bounding rect
    tooltipEl.style.display = 'block';
    tooltipEl.style.top = '0px';
    tooltipEl.style.left = '0px';

    const targetRect = target.getBoundingClientRect();
    const tooltipRect = tooltipEl.getBoundingClientRect();

    const padding = 12;
    const gap = 8;

    // Calculate vertical position (prefer above target)
    let top = targetRect.top - tooltipRect.height - gap;
    if (top < padding) {
        // If not enough room on top, flip to below target
        top = targetRect.bottom + gap;
    }

    // Calculate horizontal position (center aligned on target)
    let left = targetRect.left + (targetRect.width / 2) - (tooltipRect.width / 2);

    // Auto-clamp within viewport boundaries
    if (left < padding) {
        left = padding;
    } else if (left + tooltipRect.width > window.innerWidth - padding) {
        left = window.innerWidth - padding - tooltipRect.width;
    }

    tooltipEl.style.top = `${Math.round(top)}px`;
    tooltipEl.style.left = `${Math.round(left)}px`;
    tooltipEl.classList.add('visible');
}

function hideTooltip() {
    if (!tooltipEl) return;
    activeTarget = null;
    tooltipEl.classList.remove('visible');
}

export { initGlobalTooltip };
