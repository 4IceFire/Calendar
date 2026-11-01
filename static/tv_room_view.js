/* Shared picture coordinates and local icons; no device or network actions. */
window.TvRoomView = (function() {
  function icon(kind) {
    const paths = {
      room: 'M3 21V3h12v18M15 9h6v12M7 7h3M7 11h3M7 15h3M1 21h22',
      tv: 'M3 4h18v13H3zM8 21h8M12 17v4',
      source: 'M4 3h16v18H4zM1 12h13M10 8l4 4-4 4',
      hdmi: 'M4 5h16v8l-4 6H8l-4-6zM8 8v4M12 8v4M16 8v4',
      power: 'M12 2v10M6 5a9 9 0 1 0 12 0',
      volume: 'M3 9h4l5-5v16l-5-5H3zM16 8a6 6 0 0 1 0 8M19 5a10 10 0 0 1 0 14',
      image: 'M3 3h18v18H3zM3 17l6-6 4 4 3-3 5 5M15 7h1'
    };
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '1.7');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', paths[kind] || paths.source); svg.appendChild(path); return svg;
  }
  function node(tag, text, cls) { const n = document.createElement(tag); n.textContent = text || ''; if (cls) n.className = cls; return n; }
  function place(element, point) {
    // Coordinate centres stay inside the canvas even at exactly 0 or 1.
    element.style.left = 'calc(var(--tv-inset-x) + (100% - 2 * var(--tv-inset-x)) * ' + point.x + ')';
    element.style.top = 'calc(var(--tv-inset-y) + (100% - 2 * var(--tv-inset-y)) * ' + point.y + ')';
  }
  function canvas(url, label) {
    const box = node('div', '', 'tv-room-picture');
    if (url) {
      const image = document.createElement('img'); image.src = url; image.alt = label + ' room picture';
      image.addEventListener('load', function() { box.style.aspectRatio = image.naturalWidth + ' / ' + image.naturalHeight; });
      image.addEventListener('error', function() { image.hidden = true; box.classList.add('tv-picture-missing'); box.appendChild(node('span', 'Room picture unavailable', 'tv-picture-caption')); });
      box.appendChild(image);
    } else box.appendChild(node('span', 'No room picture configured', 'tv-picture-caption'));
    return box;
  }
  return {icon: icon, node: node, place: place, canvas: canvas};
})();
