function changeSummarizationStyle(event) {
  // get the form element
  const form = document.getElementById("summarization-style-form");

  // get the data
  const formData = new FormData(form);

  // get the "filter" field from the form data
  const filter = formData.get("filter");

  // the URL we are currently is of the form:
  // /foo/bar/previous-filter/
  // so we want to replace the "previous-filter" part with the new filter
  const currentPathname = window.location.pathname;
  const newPathname = currentPathname.replace(/\/[^\/]*\/$/, `/${filter}/`);

  // go to it!
  window.location.pathname = newPathname;
}


function doNothing(event) {
  event.preventDefault();
  event.stopPropagation();
}


function showSummarizationStyleForm() {
  // get the form element
  const form = document.getElementById("summarization-style-form");

  // remove the 'hidden' class from the form
  form.classList.remove("hidden");
}


function listenForKeyboardEvents(event) {
  // check to see if the user pressed Option+Shift+S
  if (event.altKey && event.shiftKey && event.code === "KeyS") {
    showSummarizationStyleForm();
  }
}


// ---- Council vote maps --------------------------------------------------------
//
// Interactive MapLibre GL choropleth — one per Council Bill.
// Districts 1-7 colored by vote; hover to see member name + vote.
// At-large members (Pos. 8-9) rendered as HTML badges above the map.
//
// Color key: green = In Favor, red = No/Against/Opposed, gray = Absent/Excused.

var DISTRICT_GEOJSON_URL =
  "https://raw.githubusercontent.com/seattleio/seattle-boundaries-data/master/data/city-council-districts.geojson";
var MAP_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
var VOTE_COLORS = { yes: "#16a34a", no: "#dc2626", absent: "#9ca3af", unknown: "#e5e7eb" };

function voteColor(v) {
  if (!v) return VOTE_COLORS.unknown;
  if (v.in_favor) return VOTE_COLORS.yes;
  if (v.opposed)  return VOTE_COLORS.no;
  if (v.absent)   return VOTE_COLORS.absent;
  return VOTE_COLORS.unknown;
}

function buildColorExpr(byDistrict) {
  var expr = ["match", ["get", "district"]];
  for (var d = 1; d <= 7; d++) expr.push(d, voteColor(byDistrict[d]));
  expr.push(VOTE_COLORS.unknown);
  return expr;
}

function computeBounds(geojson) {
  var w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  geojson.features.forEach(function (f) {
    var rings = f.geometry.type === "MultiPolygon"
      ? f.geometry.coordinates.reduce(function (a, p) { return a.concat(p[0]); }, [])
      : f.geometry.coordinates[0];
    rings.forEach(function (c) {
      if (c[0] < w) w = c[0]; if (c[1] < s) s = c[1];
      if (c[0] > e) e = c[0]; if (c[1] > n) n = c[1];
    });
  });
  return [[w, s], [e, n]];
}

function enrichGeoJSON(geojson, byDistrict) {
  return {
    type: "FeatureCollection",
    features: geojson.features.map(function (f) {
      var d = f.properties.district;
      var v = byDistrict[d];
      var vtype = v ? (v.in_favor ? "yes" : v.opposed ? "no" : v.absent ? "absent" : "unknown") : "unknown";
      return Object.assign({}, f, {
        id: d,
        properties: Object.assign({}, f.properties, {
          member_name: v ? v.name : "",
          vote_text: v ? v.vote : "",
          vote_type: vtype,
        }),
      });
    }),
  };
}

function initBillMap(canvas, baseGeoJSON) {
  var votes;
  try { votes = JSON.parse(canvas.dataset.votes || "[]"); } catch (e) { return; }

  var hasVotes = votes.length > 0;
  var byDistrict = {};
  if (hasVotes) {
    votes.forEach(function (v) { if (typeof v.district === "number") byDistrict[v.district] = v; });
  }

  var geojson = enrichGeoJSON(baseGeoJSON, byDistrict);
  var bounds  = computeBounds(geojson);

  var map = new maplibregl.Map({
    container: canvas,
    style: MAP_STYLE,
    bounds: bounds,
    fitBoundsOptions: { padding: 24, animate: false },
    attributionControl: false,
    scrollZoom: false,
    boxZoom: false,
    dragRotate: false,
    dragPan: false,
    keyboard: false,
    doubleClickZoom: false,
    touchZoomRotate: false,
  });

  var popup = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    className: "vote-popup",
    offset: 8,
  });
  var hoveredId = null;

  map.on("load", function () {
    map.addSource("districts", { type: "geojson", data: geojson, generateId: false });

    // Colored district fills (all grey when no votes)
    map.addLayer({
      id: "district-fills",
      type: "fill",
      source: "districts",
      paint: {
        "fill-color": hasVotes ? buildColorExpr(byDistrict) : VOTE_COLORS.unknown,
        "fill-opacity": ["case", ["boolean", ["feature-state", "hover"], false], 0.85, 0.60],
      },
    });

    // Hover darkening overlay
    map.addLayer({
      id: "district-hover",
      type: "fill",
      source: "districts",
      paint: {
        "fill-color": "#000",
        "fill-opacity": ["case", ["boolean", ["feature-state", "hover"], false], 0.12, 0],
      },
    });

    // District outlines
    map.addLayer({
      id: "district-outlines",
      type: "line",
      source: "districts",
      paint: { "line-color": "#fff", "line-width": 1.5 },
    });

    // District number labels
    map.addLayer({
      id: "district-labels",
      type: "symbol",
      source: "districts",
      layout: {
        "text-field": ["get", "district"],
        "text-size": 11,
        "text-font": ["Noto Sans Regular"],
        "text-anchor": "center",
      },
      paint: { "text-color": "#1f2937", "text-halo-color": "#fff", "text-halo-width": 1.5 },
    });

    if (hasVotes) {
      // Hover: highlight district + show popup with member name/vote
      map.on("mousemove", "district-fills", function (ev) {
        map.getCanvas().style.cursor = "pointer";
        if (hoveredId !== null) {
          map.setFeatureState({ source: "districts", id: hoveredId }, { hover: false });
        }
        hoveredId = ev.features[0].id;
        map.setFeatureState({ source: "districts", id: hoveredId }, { hover: true });

        var p = ev.features[0].properties;
        var cls = p.vote_type === "yes" ? "vp-yes" : p.vote_type === "no" ? "vp-no" : "vp-absent";
        popup.setLngLat(ev.lngLat).setHTML(
          '<div class="vp-district">District ' + p.district + "</div>" +
          (p.member_name
            ? '<div class="vp-name">'  + p.member_name + "</div>" +
              '<div class="vp-vote ' + cls + '">' + (p.vote_text || "Unknown") + "</div>"
            : '<div class="vp-vote vp-absent">No data</div>')
        ).addTo(map);
      });

      map.on("mouseleave", "district-fills", function () {
        map.getCanvas().style.cursor = "";
        if (hoveredId !== null) {
          map.setFeatureState({ source: "districts", id: hoveredId }, { hover: false });
        }
        hoveredId = null;
        popup.remove();
      });
    } else {
      // Pending state: overlay label
      var overlay = document.createElement("div");
      overlay.className = "bill-map-pending-overlay";
      var label = document.createElement("div");
      label.className = "bill-map-pending-text";
      label.textContent = "Voting upcoming \u2014 bill is currently in Committee";
      overlay.appendChild(label);
      canvas.appendChild(overlay);
    }
  });
}

function initAllBillMaps() {
  if (typeof maplibregl === "undefined") return;
  var canvases = document.querySelectorAll(".bill-map-canvas[data-votes]");
  if (!canvases.length) return;
  fetch(DISTRICT_GEOJSON_URL)
    .then(function (r) { return r.json(); })
    .then(function (geojson) { canvases.forEach(function (c) { initBillMap(c, geojson); }); })
    .catch(function (err) { console.warn("Could not load Seattle district GeoJSON:", err); });
}

document.addEventListener("DOMContentLoaded", initAllBillMaps);


// Intro panel chevron: smooth-scroll to the bills section on click.
document.addEventListener("DOMContentLoaded", function () {
  var chevron = document.getElementById("intro-chevron");
  if (chevron) {
    chevron.addEventListener("click", function () {
      var target = document.getElementById("main-content");
      if (target) {
        target.scrollIntoView({ behavior: "smooth" });
      }
    });
  }
});


// When the document is ready, make sure the summarization style is selected
// correctly, and set up the event handler for when it changes. Use basic
// javascript; no jQuery.
document.addEventListener("DOMContentLoaded", function () {
  // get the current filter from the URL. It will be the final path component
  // of the URL, so split the URL on "/" and get the last element
  const splits = window.location.pathname.split("/");
  let filter = splits[splits.length - 2];

  // make sure it is one of the valid filters, which are:
  // `concise` <-- that's it, for the moment!
  if (!["concise"].includes(filter)) {
    // if it is not one of the valid filters, default to `concise`
    filter = "concise";
  }

  // get the form element
  const form = document.getElementById("summarization-style-form");

  // select the correct option under the "filter" select element
  form.elements["filter"].value = filter;

  // set up the event handler for when the form is submitted
  form.addEventListener("submit", doNothing);

  // set up the event handler for when the form is changed
  form.addEventListener("change", changeSummarizationStyle);

  // set up a listener for keyboard up events
  document.addEventListener("keydown", listenForKeyboardEvents);
});

