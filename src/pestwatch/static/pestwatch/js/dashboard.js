/* PestWatch dashboard: risk map with zonal statistics. */
(function () {
  "use strict";

  var cfg = JSON.parse(document.getElementById("pw-config").textContent);
  var CLASS_COLORS = cfg.classes.map(function (c) { return c.color; });
  var CLASS_BY_VALUE = {};
  cfg.classes.forEach(function (c) { CLASS_BY_VALUE[String(c.value)] = c; });

  var state = {
    catalog: null,
    county: null,      // county id
    pest: null,        // pest slug
    product: null,     // "hybrid" | "classified"
    layerId: null,
    level: "ward",
    choropleth: false,
    countyLines: true, // draw the county outline
    unitLines: true,   // draw the unit (ward/constituency/county) lines
    selected: null,    // boundary id
    sort: "high",
    stats: null,
  };
  var cache = {boundaries: {}, stats: {}};

  var el = {
    county: document.getElementById("pw-county"),
    pest: document.getElementById("pw-pest"),
    product: document.getElementById("pw-product"),
    period: document.getElementById("pw-period"),
    periodWrap: document.getElementById("pw-period-wrap"),
    level: document.getElementById("pw-level"),
    choropleth: document.getElementById("pw-choropleth"),
    legend: document.getElementById("pw-legend"),
    opacityWrap: document.getElementById("pw-opacity-wrap"),
    opacity: document.getElementById("pw-opacity"),
    notice: document.getElementById("pw-notice"),
    stats: document.getElementById("pw-stats"),
  };

  // --- Helpers -------------------------------------------------------------

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function pct(x) { return x == null ? "—" : Math.round(x * 100) + "%"; }
  function fmt(x, product) {
    if (x == null) return "—";
    return product === "classified" ? x.toFixed(1) : x.toFixed(2);
  }
  function getJSON(url) {
    return fetch(url, {credentials: "same-origin"}).then(function (r) {
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    });
  }
  function qs(params) {
    return Object.keys(params).filter(function (k) { return params[k] != null && params[k] !== ""; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
  }
  function byId(list, id) {
    for (var i = 0; i < list.length; i++) if (String(list[i].id) === String(id)) return list[i];
    return null;
  }
  function levelLabel(level, plural) {
    var map = {ward: ["Ward", "Wards"], constituency: ["Constituency", "Constituencies"], county: ["County", "Counties"]};
    return (map[level] || [level, level])[plural ? 1 : 0];
  }

  // Ordered distribution keys and their colours (same palette for both products).
  function distKeys(product) {
    return product === "classified" ? cfg.classes.map(function (c) { return String(c.value); }) : cfg.hybridBins;
  }
  function distLabel(product, key) {
    return product === "classified" ? (CLASS_BY_VALUE[key] || {label: key}).label : key;
  }
  function distColor(product, key) {
    var i = distKeys(product).indexOf(key);
    return CLASS_COLORS[Math.min(Math.max(i, 0), CLASS_COLORS.length - 1)];
  }
  function meanColor(mean, product) {
    if (mean == null) return "#cccccc";
    if (product === "classified") {
      var c = CLASS_BY_VALUE[String(Math.round(mean))];
      return c ? c.color : "#cccccc";
    }
    return CLASS_COLORS[Math.min(CLASS_COLORS.length - 1, Math.floor(mean * CLASS_COLORS.length))];
  }

  // --- URL hash state --------------------------------------------------------

  function readHash() {
    var out = {};
    window.location.hash.replace(/^#/, "").split("&").forEach(function (pair) {
      var kv = pair.split("=");
      if (kv[0]) out[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || "");
    });
    return out;
  }
  function writeHash() {
    var h = qs({county: state.county, pest: state.pest, product: state.product, layer: state.layerId,
                level: state.level, shade: state.choropleth ? 1 : null,
                county_lines: state.countyLines ? null : 0, unit_lines: state.unitLines ? null : 0,
                unit: state.selected});
    history.replaceState(null, "", "#" + h);
  }

  // --- Map -------------------------------------------------------------------

  var map = L.map("pw-map", {zoomControl: true, preferCanvas: false}).setView([0.2, 37.9], 7);
  // GeoNode pages send "Referrer-Policy: same-origin", so tile requests to other
  // hosts carry no Referer, and OpenStreetMap answers those with a 403 "Access
  // blocked" tile. Send just the site's origin for basemap tiles.
  var TILE_REFERRER = "strict-origin-when-cross-origin";
  var basemaps = {
    "Streets": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, referrerPolicy: TILE_REFERRER,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}),
    "Satellite": L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 19, referrerPolicy: TILE_REFERRER, attribution: "Imagery &copy; Esri"}),
  };
  basemaps.Streets.addTo(map);
  // The units stay on the map for hover, click and shading, so these empty
  // layers only give the boundary lines their checkboxes in the layers control.
  var countyToggle = L.layerGroup().addTo(map);
  var unitsToggle = L.layerGroup().addTo(map);
  var unitsToggleName = null;
  var layersControl = L.control.layers(basemaps, {"County": countyToggle}, {position: "topleft"}).addTo(map);
  map.on("overlayadd overlayremove", function (e) {
    var on = e.type === "overlayadd";
    if (e.layer === countyToggle) {
      state.countyLines = on; showCounty();
    } else if (e.layer === unitsToggle) {
      state.unitLines = on; restyleUnits();
    } else {
      return;
    }
    writeHash();
  });

  // Name the units checkbox after the current level ("Wards", "Constituencies", ...).
  function labelUnitsToggle() {
    var name = levelLabel(state.level, true);
    if (name === unitsToggleName) return;
    if (unitsToggleName) layersControl.removeLayer(unitsToggle);
    layersControl.addOverlay(unitsToggle, name);
    unitsToggleName = name;
  }
  L.control.scale({imperial: false}).addTo(map);
  window.addEventListener("pestwatch:resize", function () { map.invalidateSize(); });

  var rasterLayer = null;
  var unitsLayer = null;
  var countyLayer = null;

  function currentLayer() {
    return state.catalog && state.layerId ? byId(state.catalog.layers, state.layerId) : null;
  }

  function showNotice(html) {
    el.notice.innerHTML = html || "";
    el.notice.hidden = !html;
  }

  function updateRaster() {
    var layer = currentLayer();
    if (rasterLayer) { map.removeLayer(rasterLayer); rasterLayer = null; }
    el.legend.hidden = true;
    el.opacityWrap.hidden = !layer || state.choropleth;
    if (!layer) return;
    if (!state.choropleth) {
      rasterLayer = L.tileLayer.wms(cfg.wmsUrl, {
        layers: layer.wmsLayer, styles: layer.style, format: "image/png", transparent: true,
        version: "1.1.1", tiled: true, opacity: el.opacity.value / 100, zIndex: 10,
      }).addTo(map);
      var legendUrl = cfg.wmsUrl + "?" + qs({
        SERVICE: "WMS", REQUEST: "GetLegendGraphic", VERSION: "1.0.0", FORMAT: "image/png",
        LAYER: layer.wmsLayer, STYLE: layer.style,
        LEGEND_OPTIONS: "fontAntiAliasing:true;fontSize:11;dx:4"});
      el.legend.innerHTML = '<div class="pw-legend-title">' + esc(productLabel(layer.product)) + '</div>' +
        '<img alt="Legend" src="' + esc(legendUrl) + '">';
      el.legend.hidden = false;
    } else {
      el.legend.innerHTML = choroplethLegend(layer.product);
      el.legend.hidden = false;
    }
  }

  function choroplethLegend(product) {
    var rows = distKeys(product).map(function (k) {
      return '<div class="pw-legend-row"><span class="pw-swatch" style="background:' + distColor(product, k) +
        '"></span>' + esc(distLabel(product, k)) + '</div>';
    }).join("");
    return '<div class="pw-legend-title">Mean ' + (product === "classified" ? "class" : "risk") + ' per ' +
      esc(levelLabel(state.level).toLowerCase()) + '</div>' + rows;
  }

  function productLabel(value) {
    var p = state.catalog.products.filter(function (x) { return x.value === value; })[0];
    return p ? p.label : value;
  }

  el.opacity.addEventListener("input", function () {
    if (rasterLayer) rasterLayer.setOpacity(el.opacity.value / 100);
  });

  function statFor(id) {
    if (!state.stats) return null;
    for (var i = 0; i < state.stats.units.length; i++) if (state.stats.units[i].id === id) return state.stats.units[i];
    return null;
  }

  function unitStyle(feature) {
    var selected = feature.id === state.selected;
    var s = {color: selected ? "#00bcd4" : "#263238", weight: selected ? 3 : 1, opacity: 0.9,
             fill: true, fillOpacity: 0, fillColor: "#ffffff"};
    // Hidden lines keep the units on the map so hover, click and shading still work.
    if (!state.unitLines && !selected) s.opacity = 0;
    if (state.choropleth && state.stats) {
      var st = statFor(feature.id);
      s.fillColor = meanColor(st && st.mean, state.stats.layer.product);
      s.fillOpacity = 0.75;
    }
    return s;
  }

  function loadBoundaries(level) {
    var key = state.county + ":" + level;
    if (!cache.boundaries[key]) {
      cache.boundaries[key] = getJSON(cfg.boundariesUrl + "?" + qs({county: state.county, level: level}));
    }
    return cache.boundaries[key];
  }

  function drawCounty() {
    if (countyLayer) { map.removeLayer(countyLayer); countyLayer = null; }
    if (!state.county) return Promise.resolve();
    return loadBoundaries("county").then(function (gj) {
      countyLayer = L.geoJSON(gj, {interactive: false,
        style: {color: "#111", weight: 2.5, fill: false, dashArray: null}});
      showCounty();
    });
  }

  function showCounty() {
    if (!countyLayer) return;
    if (state.countyLines) {
      countyLayer.addTo(map);
      countyLayer.bringToFront();
    } else {
      map.removeLayer(countyLayer);
    }
  }

  function drawUnits() {
    labelUnitsToggle();
    if (unitsLayer) { map.removeLayer(unitsLayer); unitsLayer = null; }
    if (!state.county) return Promise.resolve();
    return loadBoundaries(state.level).then(function (gj) {
      unitsLayer = L.geoJSON(gj, {
        style: unitStyle,
        onEachFeature: function (feature, layer) {
          layer.on("mouseover", function () {
            if (feature.id !== state.selected) layer.setStyle({weight: 2.5, color: "#00838f"});
          });
          layer.on("mouseout", function () { unitsLayer.resetStyle(layer); });
          layer.bindTooltip(function () { return tooltipHtml(feature); }, {sticky: true, direction: "top"});
          layer.on("click", function (e) { selectUnit(feature.id, false); queryPixel(e.latlng, feature); });
        },
      }).addTo(map);
      if (countyLayer && state.countyLines) countyLayer.bringToFront();
    });
  }

  function tooltipHtml(feature) {
    var st = statFor(feature.id);
    var product = state.stats && state.stats.layer.product;
    var html = "<strong>" + esc(feature.properties.name) + "</strong>";
    if (feature.properties.parent) html += '<br><span class="pw-subtle">' + esc(feature.properties.parent) + "</span>";
    if (st) {
      html += "<br>Mean: " + fmt(st.mean, product) + " · High risk: " + pct(st.highShare);
    }
    return html;
  }

  // Pixel value under a click, via WMS GetFeatureInfo.
  function queryPixel(latlng, feature) {
    var layer = currentLayer();
    if (!layer) return;
    var size = map.getSize();
    var bounds = map.getBounds();
    var sw = map.options.crs.project(bounds.getSouthWest());
    var ne = map.options.crs.project(bounds.getNorthEast());
    var point = map.latLngToContainerPoint(latlng).round();
    var url = cfg.wmsUrl + "?" + qs({
      SERVICE: "WMS", VERSION: "1.1.1", REQUEST: "GetFeatureInfo", LAYERS: layer.wmsLayer,
      QUERY_LAYERS: layer.wmsLayer, STYLES: layer.style, SRS: "EPSG:3857",
      BBOX: [sw.x, sw.y, ne.x, ne.y].join(","), WIDTH: size.x, HEIGHT: size.y,
      X: point.x, Y: point.y, INFO_FORMAT: "application/json", FEATURE_COUNT: 1});
    var title = feature ? "<strong>" + esc(feature.properties.name) + "</strong><br>" : "";
    var popup = L.popup().setLatLng(latlng).setContent(title + '<span class="pw-subtle">Reading value…</span>').openOn(map);
    getJSON(url).then(function (data) {
      var props = data.features && data.features[0] && data.features[0].properties;
      var value = props ? props[Object.keys(props)[0]] : null;
      var text;
      if (value == null || isNaN(value)) {
        text = "No data at this point";
      } else if (layer.product === "classified") {
        var c = CLASS_BY_VALUE[String(Math.round(value))];
        text = "Class: <strong>" + esc(c ? c.label : value) + "</strong>";
      } else {
        text = "Risk: <strong>" + Number(value).toFixed(3) + "</strong>";
      }
      popup.setContent(title + text);
    }).catch(function () {
      popup.setContent(title + '<span class="pw-subtle">Value unavailable</span>');
    });
  }

  function zoomToCounty() {
    var county = byId(state.catalog.counties, state.county);
    if (county && county.bbox) {
      map.fitBounds([[county.bbox[1], county.bbox[0]], [county.bbox[3], county.bbox[2]]], {padding: [20, 20]});
    }
  }

  function zoomToUnit(id) {
    if (!unitsLayer) return;
    unitsLayer.eachLayer(function (l) {
      if (l.feature.id === id) map.fitBounds(l.getBounds(), {padding: [40, 40], maxZoom: 13});
    });
  }

  function restyleUnits() {
    if (unitsLayer) unitsLayer.setStyle(unitStyle);
  }

  // --- Controls --------------------------------------------------------------

  function layersFor(county, pest, product) {
    return state.catalog.layers.filter(function (l) {
      return String(l.county) === String(county) && (!pest || l.pest === pest) && (!product || l.product === product);
    });
  }

  function fillSelect(select, options, value) {
    select.innerHTML = options.map(function (o) {
      return '<option value="' + esc(o.value) + '"' + (String(o.value) === String(value) ? " selected" : "") +
        (o.disabled ? " disabled" : "") + ">" + esc(o.label) + "</option>";
    }).join("");
  }

  function fillButtons(group, options, value, onPick) {
    group.innerHTML = "";
    options.forEach(function (o) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "pw-segmented-btn" + (o.value === value ? " active" : "");
      b.textContent = o.label;
      b.disabled = !!o.disabled;
      b.setAttribute("aria-pressed", String(o.value === value));
      b.addEventListener("click", function () { onPick(o.value); });
      group.appendChild(b);
    });
  }

  function fmtDate(iso, opts) {
    return new Date(iso + "T00:00:00").toLocaleDateString(undefined, opts);
  }
  function periodLabel(l) {
    if (!l.validFrom) return l.title;
    if (l.temporal === "nowcast") {
      return fmtDate(l.validFrom, {day: "numeric", month: "short", year: "numeric"});
    }
    var label = fmtDate(l.validFrom, {month: "short", year: "numeric"});
    if (l.temporal === "forecast") {
      return l.issued ? label + " (issued " + fmtDate(l.issued, {month: "short", year: "numeric"}) + ")" : label;
    }
    var to = l.validTo && l.validTo.slice(0, 7) !== l.validFrom.slice(0, 7) ? fmtDate(l.validTo, {month: "short", year: "numeric"}) : "";
    return (to ? label + " – " + to : label) + " (baseline)";
  }

  // Keep every selection valid, falling back to the first option that has data.
  function resolve() {
    var c = state.catalog;
    if (!byId(c.counties, state.county)) {
      var withData = c.counties.filter(function (x) { return layersFor(x.id).length; })[0];
      state.county = (withData || c.counties[0] || {}).id || null;
    }
    var countyLayers = layersFor(state.county);
    if (!countyLayers.some(function (l) { return l.pest === state.pest; })) {
      state.pest = countyLayers.length ? countyLayers[0].pest : (c.pests[0] || {}).slug;
    }
    var pestLayers = layersFor(state.county, state.pest);
    if (!pestLayers.some(function (l) { return l.product === state.product; })) {
      var classified = pestLayers.some(function (l) { return l.product === "classified"; });
      state.product = classified ? "classified" : (pestLayers[0] || {product: "classified"}).product;
    }
    var candidates = layersFor(state.county, state.pest, state.product);
    if (!byId(candidates, state.layerId)) state.layerId = candidates.length ? candidates[0].id : null;
  }

  function renderControls() {
    var c = state.catalog;
    fillSelect(el.county, c.counties.length ? c.counties.map(function (x) {
      return {value: x.id, label: x.name + (layersFor(x.id).length ? "" : " (no data)")};
    }) : [{value: "", label: "No counties yet"}], state.county);
    el.county.disabled = !c.counties.length;

    fillSelect(el.pest, c.pests.map(function (p) {
      return {value: p.slug, label: p.name + (layersFor(state.county, p.slug).length ? "" : " (no data)")};
    }), state.pest);

    fillButtons(el.product, c.products.map(function (p) {
      return {value: p.value, label: p.value === "hybrid" ? "Hybrid" : "Classified",
              disabled: !layersFor(state.county, state.pest, p.value).length};
    }), state.product, function (v) { state.product = v; state.layerId = null; refresh(); });

    var periods = layersFor(state.county, state.pest, state.product);
    fillSelect(el.period, periods.map(function (l) { return {value: l.id, label: periodLabel(l)}; }), state.layerId);
    el.periodWrap.hidden = !periods.length;

    fillButtons(el.level, c.levels.map(function (l) { return {value: l.value, label: l.label}; }),
      state.level, function (v) { state.level = v; state.selected = null; refresh({units: true}); });

    el.choropleth.checked = state.choropleth;
  }

  el.county.addEventListener("change", function () {
    state.county = Number(el.county.value); state.selected = null; state.layerId = null;
    refresh({county: true});
  });
  el.pest.addEventListener("change", function () { state.pest = el.pest.value; state.layerId = null; refresh(); });
  el.period.addEventListener("change", function () { state.layerId = Number(el.period.value); refresh(); });
  el.choropleth.addEventListener("change", function () {
    state.choropleth = el.choropleth.checked; updateRaster(); restyleUnits(); writeHash();
  });
  // --- Statistics panel ------------------------------------------------------

  function loadStats() {
    var layer = currentLayer();
    if (!layer) return Promise.resolve(null);
    var key = layer.id + ":" + state.level;
    if (!cache.stats[key]) cache.stats[key] = getJSON(cfg.statsUrl + "?" + qs({layer: layer.id, level: state.level}));
    return cache.stats[key];
  }

  function distributionBar(stat, product) {
    if (!stat || !stat.validCount) return '<p class="pw-subtle">No valid pixels.</p>';
    var keys = distKeys(product);
    var segs = keys.map(function (k) {
      var share = (stat.distribution[k] || 0) / stat.validCount;
      if (!share) return "";
      return '<span class="pw-seg" style="width:' + (share * 100).toFixed(2) + "%;background:" + distColor(product, k) +
        '" title="' + esc(distLabel(product, k)) + ": " + pct(share) + '"></span>';
    }).join("");
    var rows = keys.slice().reverse().map(function (k) {
      var share = (stat.distribution[k] || 0) / stat.validCount;
      return '<li><span class="pw-swatch" style="background:' + distColor(product, k) + '"></span>' +
        '<span class="pw-dist-label">' + esc(distLabel(product, k)) + '</span>' +
        '<span class="pw-dist-value">' + pct(share) + "</span></li>";
    }).join("");
    return '<div class="pw-stack" role="img" aria-label="Distribution">' + segs + "</div>" +
      '<ul class="pw-dist-list">' + rows + "</ul>";
  }

  function dominant(stat, product) {
    if (!stat || !stat.validCount) return null;
    var best = null;
    distKeys(product).forEach(function (k) {
      if (best === null || (stat.distribution[k] || 0) > (stat.distribution[best] || 0)) best = k;
    });
    return best;
  }

  function kpis(stat, product) {
    if (!stat) return "";
    var tiles = product === "classified" ? [
      ["High + very high", pct(stat.highShare)],
      ["Most common", esc(distLabel(product, dominant(stat, product)) || "—")],
      ["Mean class", fmt(stat.mean, product)],
    ] : [
      ["Mean risk", fmt(stat.mean, product)],
      ["Share ≥ 0.6", pct(stat.highShare)],
      ["Highest", fmt(stat.maximum, product)],
    ];
    return '<div class="pw-kpis">' + tiles.map(function (t) {
      return '<div class="pw-kpi"><div class="pw-kpi-value">' + t[1] + '</div><div class="pw-kpi-label">' + t[0] + "</div></div>";
    }).join("") + "</div>";
  }

  function sortedUnits(units) {
    var list = units.slice();
    if (state.sort === "name") list.sort(function (a, b) { return a.name.localeCompare(b.name); });
    else {
      var k = state.sort === "mean" ? "mean" : "highShare";
      list.sort(function (a, b) { return (b[k] == null ? -1 : b[k]) - (a[k] == null ? -1 : a[k]); });
    }
    return list;
  }

  function unitRows(stats) {
    var product = stats.layer.product;
    var metric = state.sort === "mean" ? "mean" : "highShare";
    return sortedUnits(stats.units).map(function (u) {
      var value = u[metric];
      var width = value == null ? 0 : (metric === "mean" && product === "classified" ? (value - 1) / 4 : value) * 100;
      var color = metric === "mean" ? meanColor(u.mean, product) : "#d7191c";
      return '<li class="pw-unit' + (u.id === state.selected ? " selected" : "") + '" data-id="' + u.id + '" tabindex="0">' +
        '<div class="pw-unit-name">' + esc(u.name) +
        (state.level === "ward" && u.parent ? '<span class="pw-subtle"> · ' + esc(u.parent) + "</span>" : "") + "</div>" +
        '<div class="pw-unit-bar"><span style="width:' + Math.max(0, Math.min(100, width)).toFixed(1) + "%;background:" + color + '"></span></div>' +
        '<div class="pw-unit-value">' + (metric === "mean" ? fmt(value, product) : pct(value)) + "</div></li>";
    }).join("");
  }

  function renderStats() {
    var layer = currentLayer();
    var c = state.catalog;
    if (!c.counties.length) {
      el.stats.innerHTML = '<div class="pw-stats-placeholder"><h3>No counties yet</h3>' +
        "<p>County, constituency and ward boundaries need to be imported before data can be shown.</p></div>";
      return;
    }
    if (!layer) {
      var county = byId(c.counties, state.county);
      el.stats.innerHTML = '<div class="pw-stats-placeholder"><h3>No data</h3><p>Nothing has been published for ' +
        esc(county ? county.name : "this county") + " yet.</p></div>";
      return;
    }
    var stats = state.stats;
    var product = layer.product;
    var pest = c.pests.filter(function (p) { return p.slug === layer.pest; })[0];
    var county = byId(c.counties, layer.county);
    var csvUrl = cfg.statsUrl + "?" + qs({layer: layer.id, level: state.level, format: "csv"});

    var html = '<header class="pw-stats-header">' +
      '<div class="pw-eyebrow">' + esc(county ? county.name : "") + " · " + esc(productLabel(product)) + "</div>" +
      "<h2>" + esc(pest ? pest.name : layer.pest) + "</h2>" +
      (layer.validFrom ? '<div class="pw-subtle">' + esc(periodLabel(layer)) + "</div>" : "") +
      "</header>";

    if (!stats || !stats.county) {
      html += '<div class="pw-stats-placeholder"><p>Statistics are not available for this layer yet. ' +
        "They are computed once the county's constituencies and wards have been imported.</p></div>";
      el.stats.innerHTML = html;
      return;
    }

    var sel = state.selected ? statFor(state.selected) : null;
    if (sel) {
      html += '<section class="pw-card pw-selected">' +
        '<div class="pw-card-head"><div><div class="pw-eyebrow">' + esc(levelLabel(sel.level)) + "</div><h3>" + esc(sel.name) + "</h3>" +
        (sel.parent ? '<div class="pw-subtle">' + esc(sel.parent) + "</div>" : "") + "</div>" +
        '<button type="button" class="btn btn-xs btn-default" id="pw-clear-sel">Clear</button></div>' +
        kpis(sel, product) + distributionBar(sel, product) + "</section>";
    }

    html += '<section class="pw-card"><div class="pw-card-head"><h3>County overview</h3></div>' +
      kpis(stats.county, product) + distributionBar(stats.county, product) + "</section>";

    html += '<section class="pw-card pw-units-card"><div class="pw-card-head"><h3>' + esc(levelLabel(state.level, true)) +
      ' <span class="pw-count">' + stats.units.length + "</span></h3>" +
      '<select id="pw-sort" class="form-control pw-select-compact" aria-label="Sort by">' +
      '<option value="high"' + (state.sort === "high" ? " selected" : "") + ">Sort: high-risk share</option>" +
      '<option value="mean"' + (state.sort === "mean" ? " selected" : "") + ">Sort: mean</option>" +
      '<option value="name"' + (state.sort === "name" ? " selected" : "") + ">Sort: name</option>" +
      "</select></div>";
    if (stats.units.length) {
      html += '<ul class="pw-units" id="pw-units">' + unitRows(stats) + "</ul>";
    } else {
      html += '<p class="pw-subtle">No ' + esc(levelLabel(state.level, true).toLowerCase()) + " have been imported for this county.</p>";
    }
    html += '<a class="pw-download" href="' + esc(csvUrl) + '"><i class="fa fa-download"></i> Download CSV</a>' +
      (layer.datasetUrl ? ' · <a class="pw-download" href="' + esc(layer.datasetUrl) + '">GeoNode page</a>' : "") +
      "</section>";

    el.stats.innerHTML = html;

    var sort = document.getElementById("pw-sort");
    if (sort) sort.addEventListener("change", function () { state.sort = sort.value; renderStats(); });
    var clear = document.getElementById("pw-clear-sel");
    if (clear) clear.addEventListener("click", function () { selectUnit(null); });
    var list = document.getElementById("pw-units");
    if (list) {
      var pick = function (e) {
        var li = e.target.closest(".pw-unit");
        if (li) selectUnit(Number(li.getAttribute("data-id")), true);
      };
      list.addEventListener("click", pick);
      list.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(e); } });
    }
  }

  function selectUnit(id, zoom) {
    state.selected = id;
    restyleUnits();
    if (unitsLayer) {
      unitsLayer.eachLayer(function (l) { if (l.feature.id === id) l.bringToFront(); });
      if (countyLayer && state.countyLines) countyLayer.bringToFront();
    }
    if (id && zoom) zoomToUnit(id);
    if (!id) map.closePopup();
    renderStats();
    writeHash();
  }

  // --- Orchestration ---------------------------------------------------------

  function refresh(opts) {
    opts = opts || {};
    resolve();
    renderControls();
    writeHash();
    updateRaster();
    showNotice(currentLayer() ? "" : (state.catalog.counties.length ? (cfg.emptyText || "No published layer for this selection.") : ""));

    var geom = Promise.resolve();
    if (opts.county || opts.initial) {
      geom = drawCounty().then(drawUnits);
      if (!opts.keepView) zoomToCounty();
    } else if (opts.units) {
      geom = drawUnits();
    }
    el.stats.classList.add("pw-loading");
    return Promise.all([geom, loadStats()]).then(function (res) {
      state.stats = res[1];
      if (state.selected && !statFor(state.selected)) state.selected = null;
      restyleUnits();
      if (state.choropleth) updateRaster();
      renderStats();
    }).catch(function (err) {
      el.stats.innerHTML = '<div class="pw-stats-placeholder"><h3>Something went wrong</h3><p>' + esc(err.message) + "</p></div>";
    }).then(function () { el.stats.classList.remove("pw-loading"); });
  }

  getJSON(cfg.catalogUrl).then(function (catalog) {
    state.catalog = catalog;
    var h = readHash();
    if (h.county) state.county = Number(h.county);
    if (h.pest) state.pest = h.pest;
    if (h.product) state.product = h.product;
    if (h.layer) state.layerId = Number(h.layer);
    if (h.level && catalog.levels.some(function (l) { return l.value === h.level; })) state.level = h.level;
    state.choropleth = h.shade === "1";
    state.countyLines = h.county_lines !== "0";
    state.unitLines = h.unit_lines !== "0";
    if (!state.countyLines) map.removeLayer(countyToggle);
    if (!state.unitLines) map.removeLayer(unitsToggle);
    if (h.unit) state.selected = Number(h.unit);
    return refresh({initial: true}).then(function () {
      if (state.selected) zoomToUnit(state.selected);
    });
  }).catch(function (err) {
    el.stats.innerHTML = '<div class="pw-stats-placeholder"><h3>Could not load PestWatch</h3><p>' + esc(err.message) + "</p></div>";
  });
})();
