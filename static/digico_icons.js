(function () {
  'use strict';

  const icons = [
    {
      id: '',
      label: 'No icon',
      markup: '<circle cx="12" cy="12" r="8.5"></circle><path d="m6 18 12-12"></path>',
    },
    {
      id: 'vocals',
      label: 'Vocals',
      markup: '<rect x="9" y="2.5" width="6" height="11" rx="3"></rect><path d="M6.5 10.5v.5a5.5 5.5 0 0 0 11 0v-.5M12 16.5v4M8.5 20.5h7"></path>',
    },
    {
      id: 'drums',
      label: 'Drums',
      markup: '<ellipse cx="12" cy="8" rx="7.5" ry="3"></ellipse><path d="M4.5 8v7c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V8M4.5 15c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3M8.5 4l7 7M15.5 4l-7 7"></path>',
    },
    {
      id: 'keyboard',
      label: 'Keyboard',
      markup: '<path d="M4.5 5h15L21 9v10H3V9z"></path><path d="M3 10h18M6.6 10v9M10.2 10v9M13.8 10v9M17.4 10v9"></path><path d="M5.8 10v4M9.4 10v4M13 10v4M16.6 10v4" stroke-width="2.4"></path><path d="M6 7.5h5M15.5 7.5h.01M18 7.5h.01"></path>',
    },
    {
      id: 'acoustic',
      label: 'Acoustic',
      markup: '<path d="m14.8 9.2 5.7-5.7M18.4 2.6l3 3M13.9 9.5c-1.4-1.4-3.5-1-4.2.6-.5 1.1-1.4 2-2.5 2.5-1.6.7-2 2.8-.6 4.2l.6.6c1.4 1.4 3.5 1 4.2-.6.5-1.1 1.4-2 2.5-2.5 1.6-.7 2-2.8.6-4.2z"></path><circle cx="10.6" cy="13.4" r="1.4"></circle><path d="m12 12 6.7-6.7"></path>',
    },
    {
      id: 'electric',
      label: 'Electric',
      markup: '<path d="m14.5 9.5 6-6M18.4 2.6l3 3M13.9 9.8c-1.4-.9-3.1-.5-4 1L9 12l-3.3-.8 1.2 3-1.8 2.3 3.3.2.4 3 2.5-1.8c1-.7 2.4-.7 3.3-1.6 1.8-1.8 1.4-4.8-.7-6.5z"></path><path d="m9.4 16 9.3-9.3M10.5 13.2l2.3 2.3M13.2 10.8l-1 2h1.6l-1.2 2.5 3-3.4H14l.9-1.7"></path>',
    },
    {
      id: 'bass',
      label: 'Bass',
      markup: '<path d="M10.5 10V2.5h3V10"></path><path d="M10.5 3H8M13.5 4.5H16M10.5 6H8M13.5 7.5H16"></path><path d="M9.8 9.5C7.4 8.7 5 10.5 5 13c0 1 .4 2 1.2 2.7-1.1 2.6.8 5.8 3.7 5.8h4.2c2.9 0 4.8-3.2 3.7-5.8.8-.7 1.2-1.7 1.2-2.7 0-2.5-2.4-4.3-4.8-3.5l-1.1.4-1.2-.4z"></path><path d="M12 2.5v17M9 18h6M9.5 13h5"></path>',
    },
    {
      id: 'speaker',
      label: 'Speaker',
      markup: '<rect x="5" y="2.5" width="14" height="19" rx="2"></rect><circle cx="12" cy="14.5" r="4"></circle><circle cx="12" cy="7" r="1.5"></circle><path d="M9.5 14.5a2.5 2.5 0 0 0 5 0"></path>',
    },
    {
      id: 'headset',
      label: 'Headset mic',
      markup: '<path d="M4 13v-2a8 8 0 0 1 16 0v2M4 12.5h2.5v6H5.8A1.8 1.8 0 0 1 4 16.7zM20 12.5h-2.5v6H19a1 1 0 0 0 1-1zM17.5 17.5c0 2-1.6 3-4.5 3"></path><circle cx="11.5" cy="20.5" r="1"></circle>',
    },
    {
      id: 'tracks',
      label: 'Tracks',
      markup: '<path d="M4 5h16M4 12h16M4 19h16"></path><path d="m6 7.5 4 2.5-4 2.5z" fill="currentColor" stroke="none"></path><path d="M12 8.5h2l1-2 1.5 5 1-3H20M12 15.5h1.5l1-2 1.5 4 1-2H20"></path>',
    },
    {
      id: 'fx',
      label: 'FX',
      markup: '<path d="M3 15c2.2-4 4.3-4 6.5 0s4.3 4 6.5 0 3.8-4 5-1.5"></path><path d="m8 3 .7 1.8L10.5 5.5l-1.8.7L8 8l-.7-1.8-1.8-.7 1.8-.7zM17 2l.5 1.3 1.3.5-1.3.5L17 5.6l-.5-1.3-1.3-.5 1.3-.5zM18.5 8l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"></path>',
    },
  ];

  const byId = new Map(icons.map(icon => [icon.id, icon]));
  const aliases = new Map([
    ['🎤', 'vocals'], ['🎙️', 'vocals'], ['🥁', 'drums'], ['🎹', 'keyboard'],
    ['🎸', 'electric'], ['🔊', 'speaker'], ['🎧', 'headset'], ['🎶', 'tracks'],
    ['🎵', 'tracks'], ['✨', 'fx'],
  ]);

  function normalize(value) {
    const raw = String(value || '').trim();
    if (byId.has(raw)) return raw;
    if (aliases.has(raw)) return aliases.get(raw);
    const lower = raw.toLowerCase();
    if (/vocal|microphone|(^|[-_/])mic/.test(lower)) return 'vocals';
    if (/drum|snare|cymbal/.test(lower)) return 'drums';
    if (/keyboard|piano|synth/.test(lower)) return 'keyboard';
    if (/acoustic/.test(lower)) return 'acoustic';
    if (/bass/.test(lower)) return 'bass';
    if (/electric|guitar/.test(lower)) return 'electric';
    if (/speaker|loudspeaker|monitor/.test(lower)) return 'speaker';
    if (/headset/.test(lower)) return 'headset';
    if (/track|tape|reel|music|note/.test(lower)) return 'tracks';
    if (/\bfx\b|effect|spark|wand/.test(lower)) return 'fx';
    return '';
  }

  function find(value) {
    return byId.get(normalize(value)) || byId.get('');
  }

  function create(value, className) {
    const entry = find(value);
    const wrap = document.createElement('span');
    wrap.className = className || 'digico-svg-icon';
    wrap.dataset.icon = entry.id;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.7');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.innerHTML = entry.markup;
    wrap.appendChild(svg);
    return wrap;
  }

  window.TDeckDigicoIcons = Object.freeze({
    items: Object.freeze(icons.map(({id, label}) => Object.freeze({id, label}))),
    normalize,
    find,
    create,
  });
})();
