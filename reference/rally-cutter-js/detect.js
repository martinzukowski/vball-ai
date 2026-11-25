// Turns the analysis samples into rallies. Pure function, no DOM, so it can be tuned offline.
//
// Measured on real match footage: overall motion between points (walking, fetching the ball)
// is about as high as during many rallies, so motion level alone can't tell them apart. What
// does stand out is the pre-serve freeze: everyone holds still for a second or two, then the
// serve goes up. The same pause happens right after a rally ends, before people start walking.
//
// 1. Serves: each ref whistle (when audible) plus the end of the first freeze after it;
//    without whistles, clear freezes followed by a jump in motion.
// 2. A rally runs from its serve until motion next settles into a pause.
// 3. Points are at least `minDead` seconds apart, so a "freeze" right after a rally is that
//    rally's ending pause, not a new serve.
// 4. Fallback for footage with no usable freezes: rallies are stretches of high motion.

(function (root) {
  const pct = (arr, q) => {
    const a = arr.filter((v) => v != null && isFinite(v)).sort((x, y) => x - y);
    return a.length ? a[Math.min(a.length - 1, Math.floor(q * (a.length - 1)))] : 0;
  };
  const smooth = (arr, k) => arr.map((_, i) => {
    let s = 0, n = 0;
    for (let j = Math.max(0, i - k); j <= Math.min(arr.length - 1, i + k); j++) if (arr[j] != null) { s += arr[j]; n++; }
    return n ? s / n : 0;
  });

  const DEFAULTS = {
    smoothSec: 0.4,     // half-width of the motion smoothing window
    freezeQ: 0.15,      // "still" = below this quantile of the footage's own motion
    freezeMin: 0.8,     // a serve freeze lasts at least this long
    riseQ: 0.5, riseMul: 1.3, riseWithin: 3, // ...and motion then climbs past median x riseMul within 3 s
    whistleLead: 7,     // the serve follows its whistle within this many seconds
    whistleDb: 10,      // whistle band must beat its typical level by this much
    endQ: 0.3,          // a rally ends when motion stays below this quantile...
    endHold: 0.8,       // ...for this long
    minRally: 2.5, maxRally: 35,
    minDead: 7,         // seconds from one rally's end to the next serve, at least
    preRoll: 2.0,       // keep this much before the serve (toss and approach)
    postRoll: 1.0,      // and this much after the ball dies
    breakSec: 40,       // gaps longer than this are labelled as breaks (timeouts, set changes)
    // fallback (no freezes found): hysteresis on motion level
    hi: 0.42, lo: 0.22, exitHold: 1.4,
  };

  // Samples arrive at slightly uneven times (they follow decoded frames), so put them on a
  // regular grid first. Grid points with no sample nearby are marked unmeasured.
  function resample(raw) {
    const step = raw.step || 0.2, m = raw.t.length;
    const out = { ...raw, t: [], act: [], wh: [], lo: [], bad: [] };
    if (!m) return out;
    const t0 = raw.t[0], n = Math.floor((raw.t[m - 1] - t0) / step) + 1;
    let j = 0;
    for (let i = 0; i < n; i++) {
      const t = t0 + i * step;
      while (j < m - 1 && raw.t[j + 1] <= t) j++;
      const k = j < m - 1 && Math.abs(raw.t[j + 1] - t) < Math.abs(raw.t[j] - t) ? j + 1 : j;
      out.t.push(+t.toFixed(2)); out.act.push(raw.act[k]); out.wh.push(raw.wh[k]); out.lo.push(raw.lo[k]);
      out.bad.push(raw.bad[k] || Math.abs(raw.t[k] - t) > 1.5 ? 1 : 0);
    }
    return out;
  }

  function detectRallies(raw, options) {
    const o = { ...DEFAULTS, ...(options || {}) };
    const f = resample(raw);
    const n = f.t.length, step = f.step || 0.2;
    if (n < 10) return { rallies: [], breaks: [], whistles: [], serves: [], debug: { t: f.t, smoothed: [] } };
    const act = smooth(f.act.map((v, i) => (f.bad[i] ? null : v)), Math.max(1, Math.round(o.smoothSec / step)));
    const idx = (t) => Math.max(0, Math.min(n - 1, Math.round((t - f.t[0]) / step)));

    // per-camera-position levels (gyms, lenses and camera heights differ)
    const camAt = (t) => { let c = 0; (f.cams || [{ from: 0 }]).forEach((cam, i) => { if (t >= cam.from) c = i; }); return c; };
    const camIdx = f.t.map(camAt), lv = {};
    for (const c of new Set(camIdx)) {
      const vals = act.filter((_, i) => camIdx[i] === c && !f.bad[i]);
      lv[c] = { still: pct(vals, o.freezeQ), end: pct(vals, o.endQ), rise: pct(vals, o.riseQ) * o.riseMul,
        quiet: pct(vals, .2), busy: pct(vals, .92) };
      lv[c].hi = lv[c].quiet + o.hi * (lv[c].busy - lv[c].quiet);
      lv[c].lo = lv[c].quiet + o.lo * (lv[c].busy - lv[c].quiet);
    }
    const L = (i) => lv[camIdx[i]];

    // whistles
    const whistles = [];
    if (f.whValid) {
      const diff = f.wh.map((w, i) => w - f.lo[i]);
      const base = pct(diff, .5), loudBand = pct(f.wh, .5);
      for (let i = 0; i < n; i++) {
        if (f.bad[i]) continue;
        if (diff[i] - base >= o.whistleDb + 8 && f.wh[i] - loudBand >= o.whistleDb) {
          if (!whistles.length || f.t[i] - whistles[whistles.length - 1] > 3) whistles.push(f.t[i]);
        }
      }
    }

    // freezes that end in a jump of motion = serve candidates (time = end of the freeze)
    const freezes = [];
    for (let i = 0; i < n;) {
      if (f.bad[i] || act[i] >= L(i).still) { i++; continue; }
      let j = i;
      while (j < n && !f.bad[j] && act[j] < L(j).still * 1.25) j++;
      if ((j - i) * step >= o.freezeMin) {
        for (let k = j; k < n && (k - j) * step <= o.riseWithin; k++) if (act[k] > L(k).rise) { freezes.push(f.t[Math.min(j, n - 1)]); break; }
      }
      i = j + 1;
    }

    // serves: whistles anchor them when the ref is audible; otherwise use freezes
    let serves;
    const useWhistles = whistles.length >= 4;
    if (useWhistles) {
      serves = whistles.map((w) => { const fz = freezes.find((x) => x >= w && x <= w + o.whistleLead); return fz != null ? fz : w + 2.5; });
      // a missed whistle: accept a freeze far from any whistle
      for (const fz of freezes) if (!whistles.some((w) => Math.abs(fz - w) < 12)) serves.push(fz);
    } else serves = freezes.slice();
    serves = serves.sort((a, b) => a - b).filter((s, i, a) => !i || s - a[i - 1] > 4);

    // rally from each serve until motion settles into a pause
    const dur = f.duration || f.t[n - 1];
    const cands = serves.map((s, si) => {
      const limit = Math.min(s + o.maxRally, si + 1 < serves.length ? serves[si + 1] - 2 : dur);
      let end = null, quietSince = null;
      for (let i = idx(s + 2); i < n && f.t[i] <= limit; i++) {
        if (f.bad[i]) { end = f.t[i]; break; }
        if (act[i] < L(i).end) { if (quietSince == null) quietSince = f.t[i]; if (f.t[i] - quietSince >= o.endHold) { end = quietSince; break; } }
        else quietSince = null;
      }
      if (end == null) end = Math.min(limit, s + 12); // no clear pause before the next serve
      const whistle = useWhistles && whistles.some((w) => s - w >= 0 && s - w <= o.whistleLead);
      return { s, end, whistle };
    }).filter((c) => c.end - c.s >= o.minRally - o.preRoll);
    // Points are at least minDead apart. When two candidates are closer than that, one is the
    // pause right after a rally (followed by a few seconds of walking): keep the whistled one,
    // otherwise the one followed by the longer burst of play.
    const chosen = [];
    for (const c of cands) {
      const last = chosen[chosen.length - 1];
      if (!last || c.s >= last.end + o.minDead) { chosen.push(c); continue; }
      // two whistled serves are both real: the earlier rally just ended sooner than it looked
      if (c.whistle && last.whistle) { last.end = Math.max(last.s + 1, Math.min(last.end, c.s - 3)); chosen.push(c); continue; }
      const better = c.whistle !== last.whistle ? c.whistle : c.end - c.s > last.end - last.s;
      const before = chosen[chosen.length - 2];
      if (better && (!before || c.s >= before.end + o.minDead)) chosen[chosen.length - 1] = c;
    }
    let rallies = chosen.map((c) => ({ start: Math.max(0, c.s - o.preRoll), end: Math.min(dur, c.end + o.postRoll), serve: +c.s.toFixed(2), whistle: c.whistle }));

    // fallback for footage without usable freezes (e.g. a busy gym in frame)
    let mode = 'serves';
    if (rallies.length < Math.max(3, dur / 120)) {
      mode = 'motion';
      rallies = [];
      let inR = false, start = 0, lowSince = null;
      const spans = [];
      for (let i = 0; i < n; i++) {
        const a = act[i], t = f.t[i], T = L(i);
        if (f.bad[i]) { if (inR) { spans.push([start, t]); inR = false; } continue; }
        if (!inR) { if (a > T.hi) { inR = true; start = t; lowSince = null; } }
        else if (a < T.lo) { if (lowSince == null) lowSince = t; if (t - lowSince >= o.exitHold) { spans.push([start, lowSince]); inR = false; } }
        else lowSince = null;
      }
      if (inR) spans.push([start, f.t[n - 1]]);
      for (const [s, e] of spans) {
        const last = rallies[rallies.length - 1];
        if (last && s - o.preRoll < last.end + 1) last.end = Math.min(dur, e + o.postRoll);
        else if (e - s >= o.minRally) rallies.push({ start: Math.max(0, s - o.preRoll), end: Math.min(dur, e + o.postRoll), whistle: false });
      }
    }

    // strength (for display) and tidy numbers
    rallies.forEach((r) => {
      let sum = 0, c = 0;
      for (let i = idx(r.start); i <= idx(r.end); i++) { sum += act[i]; c++; }
      const T = L(idx(r.start));
      r.score = +Math.max(0, Math.min(1, (sum / Math.max(1, c) - T.quiet) / Math.max(1e-6, T.busy - T.quiet))).toFixed(2);
      r.start = +r.start.toFixed(2); r.end = +r.end.toFixed(2);
    });
    for (let i = 1; i < rallies.length; i++) if (rallies[i].start < rallies[i - 1].end) {
      const mid = +((rallies[i].start + rallies[i - 1].end) / 2).toFixed(2);
      rallies[i - 1].end = mid; rallies[i].start = mid;
    }

    const breaks = [];
    let pe = 0;
    for (const r of rallies.concat([{ start: dur }])) {
      if (r.start - pe >= o.breakSec) breaks.push({ start: +pe.toFixed(2), end: +r.start.toFixed(2) });
      pe = r.end;
    }
    return { rallies, breaks, whistles, serves, mode, debug: { levels: lv, t: f.t, smoothed: act } };
  }

  root.detectRallies = detectRallies;
  root.DETECT_DEFAULTS = DEFAULTS;
  if (typeof module !== 'undefined') module.exports = { detectRallies, DEFAULTS };
})(typeof window !== 'undefined' ? (window.VB = window.VB || {}) : globalThis);
