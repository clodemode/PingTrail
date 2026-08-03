/* PING_TRAIL attribution chart.
 *
 * STACKED BAR on a CATEGORY axis. Not a line, not an area, not a time axis.
 * Kris, 2026-08-01: "you cannot assume response time between pings." A line or
 * area silently interpolates latency across the gap between ticks, inventing
 * measurements that were never taken. Bars assert only what was sampled, and a
 * category axis keeps evenly-spaced bars from implying continuity across a
 * collection gap.
 *
 * Three rules this file exists to honour:
 *   1. A fully-timed-out tick is a VOID bar — every series is null there, so
 *      nothing is drawn and nothing bridges to the neighbouring tick.
 *   2. Negative segment deltas are NEVER clamped to zero. They stack below the
 *      axis with their sign intact and carry an explicit inversion marker.
 *   3. Stacked totals come from the pre-computed `totals` array, never p.value —
 *      a stacked label formatter only ever sees ONE segment
 *      (echarts-stacked-label-total).
 *
 * RE-ENTRANT. The chart lives inside an HTMX-polled region, so this file is
 * asked to build the chart again every time fresh ticks land. `initChart` is
 * exported on window and disposes the previous instance first — an undisposed
 * ECharts instance holds its old DOM node and its resize listener alive, and
 * once every 10 seconds that adds up fast.
 */
(function () {
  "use strict";

  var current = null;
  var resizeBound = false;

  function init() {
    var node = document.getElementById("attribution-chart");
    if (!node || typeof echarts === "undefined") return;

    var payloadEl = document.getElementById("chart-data");
    if (!payloadEl) return;
    var payload = JSON.parse(payloadEl.textContent);

    // The polled swap replaced the old node; drop the instance bound to it.
    if (current) {
      try { current.dispose(); } catch (e) { /* already gone */ }
      current = null;
    }

    var categories = payload.categories || [];
    var segments = payload.series || [];
    // Pre-computed bucket totals — referenced from the formatter closure.
    var totals = payload.totals || [];
    var inversions = payload.inversions || [];
    var voids = payload.voids || [];

    var chart = echarts.init(node, null, { renderer: "canvas" });
    current = chart;
    var voidSet = new Set(voids);

    var series = segments.map(function (segment) {
      return {
        name: segment.label,
        type: "bar",
        stack: "attribution",
        // A void tick's data is null (not 0) so ECharts draws nothing at all.
        data: segment.data,
        itemStyle: { color: segment.colour, borderWidth: 0 },
        emphasis: { focus: "series" },
        barMaxWidth: 34,
      };
    });

    // Total label carrier: plotted AT the true total (= RTT to the deepest
    // answering rung), so the label is correct even when an inversion pushes
    // part of the stack below the axis.
    series.push({
      name: "total",
      type: "scatter",
      // Hiding this carrier is fiddlier than it looks (ECharts 6.1.0):
      //   symbol:"none"        -> suppresses the LABEL as well as the symbol
      //   itemStyle.opacity:0  -> ALSO suppresses the label (opacity cascades)
      // A transparent FILL is the one form that keeps the label painted.
      symbol: "circle",
      symbolSize: 1,
      itemStyle: { color: "rgba(0,0,0,0)", borderWidth: 0 },
      silent: true,
      data: totals.map(function (value, index) {
        return value === null || value === undefined ? null : [index, value];
      }),
      label: {
        show: true,
        position: "top",
        distance: 4,
        fontSize: 9,
        color: "#495057",
        formatter: function (p) {
          // Bucket total from the PRE-COMPUTED array — p.value in a stacked
          // formatter would give one segment (echarts-stacked-label-total).
          var total = totals[p.dataIndex];
          return total === null || total === undefined ? "" : total.toFixed(0);
        },
      },
      // Drop labels that would collide rather than overprinting them.
      labelLayout: { hideOverlap: true },
    });

    // Inversion markers — a deeper rung answered FASTER than a shallower one.
    // Diagnostic, not noise; surfaced, never clamped away.
    if (inversions.length) {
      series.push({
        name: "inversion",
        type: "scatter",
        symbol: "diamond",
        symbolSize: 11,
        z: 12,
        data: inversions.map(function (row) {
          return { value: [row[0], row[1]], segmentLabel: row[2] };
        }),
        itemStyle: { color: "#d6336c", borderColor: "#fff", borderWidth: 1.5 },
        tooltip: { show: false },
      });
    }

    // Void ticks — mark the gap explicitly so "nothing answered" is visibly
    // different from "no data collected".
    if (voids.length) {
      series.push({
        name: "void",
        type: "scatter",
        symbol: "circle",
        symbolSize: 9,
        z: 12,
        data: voids.map(function (index) {
          return [index, 0];
        }),
        itemStyle: { color: "#fff", borderColor: "#dc3545", borderWidth: 2 },
        tooltip: { show: false },
      });
    }

    var option = {
      // Headroom at the top so the total labels are not clipped by the grid.
      grid: { left: 54, right: 16, top: 42, bottom: 58 },
      legend: {
        top: 0,
        type: "scroll",
        itemWidth: 12,
        itemHeight: 8,
        textStyle: { fontSize: 11 },
        data: segments.map(function (s) {
          return s.label;
        }),
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        confine: true,
        formatter: function (params) {
          if (!params.length) return "";
          var index = params[0].dataIndex;
          var lines = ['<strong>' + categories[index] + "</strong>"];

          if (voidSet.has(index)) {
            lines.push(
              '<span style="color:#dc3545">VOID tick — every rung timed out.</span>'
            );
            lines.push(
              '<span style="color:#6c757d;font-size:11px">No value interpolated across this gap.</span>'
            );
            return lines.join("<br>");
          }

          params.forEach(function (p) {
            if (p.seriesName === "total" || p.seriesName === "inversion" || p.seriesName === "void") return;
            var value = p.value;
            if (value === null || value === undefined) return;
            var inverted = value < 0;
            var colour = inverted ? "#d6336c" : p.color;
            lines.push(
              '<span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:' +
                colour +
                ';margin-right:5px"></span>' +
                p.seriesName +
                ": <strong>" +
                value.toFixed(3) +
                " ms</strong>" +
                (inverted ? ' <em style="color:#d6336c">inversion</em>' : "")
            );
          });

          var total = totals[index];
          if (total !== null && total !== undefined) {
            lines.push(
              '<span style="border-top:1px solid #dee2e6;display:block;margin-top:4px;padding-top:4px">' +
                "total (deepest rung): <strong>" +
                total.toFixed(3) +
                " ms</strong></span>"
            );
          }
          lines.push('<span style="color:#6c757d;font-size:11px">click for raw RTT detail</span>');
          return lines.join("<br>");
        },
      },
      xAxis: {
        // CATEGORY, never time — evenly-spaced discrete sample ticks.
        type: "category",
        data: categories,
        axisLabel: { fontSize: 10, rotate: 55, hideOverlap: true },
        axisTick: { alignWithLabel: true },
      },
      yAxis: {
        type: "value",
        name: "ms",
        nameTextStyle: { fontSize: 10, color: "#6c757d" },
        axisLabel: { fontSize: 10 },
        splitLine: { lineStyle: { color: "#e9ecef" } },
      },
      series: series,
    };

    chart.setOption(option);

    // Per-tick raw-RTT detail expands INLINE via HTMX — no modal
    // (spec-no-modals-htmx-inline-expansion).
    chart.on("click", function (params) {
      var sampleIds = payload.sample_ids || [];
      var sampleId = sampleIds[params.dataIndex];
      if (!sampleId || typeof htmx === "undefined") return;
      var url = node.dataset.detailBase + sampleId + "/";
      htmx.ajax("GET", url, { target: "#panel-tick", swap: "innerHTML" });
      var panel = document.getElementById("panel-tick");
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });

    // Bound once, against whichever instance is current — re-binding on every
    // poll would stack listeners.
    if (!resizeBound) {
      resizeBound = true;
      window.addEventListener("resize", function () {
        if (current) current.resize();
      });
    }
  }

  // Exported so the polled chart partial can re-init itself after each swap.
  window.PingTrail = window.PingTrail || {};
  window.PingTrail.initChart = init;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
