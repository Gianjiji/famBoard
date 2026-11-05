/* Tema condiviso per i grafici Chart.js di famboard.
   Caricato in <head> dopo chart.js, prima degli script di pagina. */
(function () {
  if (!window.Chart) return;
  var css = getComputedStyle(document.documentElement);
  function v(name, fallback) {
    var x = css.getPropertyValue(name).trim();
    return x || fallback;
  }
  var fg = v('--fg', '#e9eef5');
  var muted = v('--muted', '#8fa0b3');
  var panel = v('--panel', '#131a23');
  var panel2 = v('--panel2', '#1b2430');
  var line = v('--line', '#243040');

  /* Coppie serie validate (CVD + contrasto) su superficie scura:
     accent #5c7cfa / danger #f03e3e e accent / night #e8590c. */
  window.fbTheme = {
    accent: v('--accent', '#5c7cfa'),
    accentFill: 'rgba(92,124,250,.14)',
    danger: v('--danger', '#f03e3e'),
    night: v('--night', '#e8590c'),
    surface: panel,
    muted: muted
  };

  var C = Chart;
  C.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
  C.defaults.font.size = 12;
  C.defaults.color = muted;

  C.defaults.scale.grid.color = 'rgba(143,160,179,.1)';
  C.defaults.scale.ticks.maxRotation = 0;
  if (C.defaults.scale.border) C.defaults.scale.border.display = false;

  var L = C.defaults.plugins.legend.labels;
  L.usePointStyle = true;
  L.pointStyle = 'circle';
  L.boxWidth = 7;
  L.boxHeight = 7;
  L.padding = 16;

  var T = C.defaults.plugins.tooltip;
  T.backgroundColor = panel2;
  T.titleColor = fg;
  T.bodyColor = fg;
  T.borderColor = line;
  T.borderWidth = 1;
  T.cornerRadius = 8;
  T.padding = 10;
  T.boxPadding = 4;
  T.usePointStyle = true;

  C.defaults.elements.line.borderWidth = 2;
  C.defaults.elements.point.radius = 0;
  C.defaults.elements.point.hitRadius = 14;
  C.defaults.elements.point.hoverRadius = 4;
  C.defaults.elements.bar.borderRadius = 3;
  C.defaults.elements.arc.borderColor = panel;
  C.defaults.elements.arc.borderWidth = 2;
  C.defaults.elements.arc.hoverOffset = 6;
})();
