/* Génesis video stage: deterministic, time-driven scenes.
 *
 * A scene page loads ../build/timeline.js (window.TIMELINE, window.RUN) and this file, then calls
 *   scene((t, S) => { ...set styles from t... })
 * The renderer calls window.__seek(t) for every frame (t in seconds, scene-local). Never use CSS
 * transitions/animations, setTimeout or Date: everything visible must be a pure function of t.
 *
 * S (helpers) = {
 *   D            scene duration (s)
 *   id           scene id ("s08")
 *   words        narration words [{w, start, end}] in scene-local seconds
 *   cue(word, n) local time when the n-th occurrence (0-based) of `word` starts being spoken
 *                (case/accents/punctuation-insensitive, prefix match); falls back to 0
 *   p(t, a, b)   clamped progress 0..1 of t between a and b
 *   ease(x) easeOut(x) easeIn(x) back(x)   easing curves on 0..1
 *   lerp(a, b, x)
 *   fade(el, x, dy=16)    opacity x and translateY((1-x)*dy px)
 *   pop(el, x)            opacity + scale from .92 with a little overshoot
 *   type(el, text, x)     typewriter: shows the first round(x*len) chars
 *   num(x, from, to, decimals=0, locale='es-CO')  formatted counter value
 *   run                   window.RUN (rehearsal data, see build/run.js)
 * }
 */
(function () {
  const TL = window.TIMELINE || { scenes: {} };
  const id = document.body.dataset.scene;
  const sc = TL.scenes[id] || { duration: 10, words: [] };
  const norm = (s) =>
    s
      .toLowerCase()
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .replace(/[^a-z0-9%]/g, "");
  const clamp = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
  const S = {
    D: sc.duration,
    id,
    words: sc.words || [],
    run: window.RUN || {},
    cue(word, n = 0) {
      const k = norm(word);
      let seen = 0;
      for (const w of S.words) {
        if (norm(w.w).startsWith(k)) {
          if (seen === n) return w.start;
          seen++;
        }
      }
      return 0;
    },
    p: (t, a, b) => clamp((t - a) / (b - a)),
    ease: (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
    easeOut: (x) => 1 - Math.pow(1 - x, 3),
    easeIn: (x) => x * x * x,
    back: (x) => {
      const c1 = 1.70158,
        c3 = c1 + 1;
      return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2);
    },
    lerp: (a, b, x) => a + (b - a) * x,
    fade(el, x, dy = 16) {
      if (!el) return;
      const e = S.easeOut(clamp(x));
      el.style.opacity = e;
      el.style.transform = `translateY(${(1 - e) * dy}px)`;
    },
    pop(el, x) {
      if (!el) return;
      const c = clamp(x);
      el.style.opacity = S.easeOut(Math.min(1, c * 1.6));
      el.style.transform = `scale(${0.92 + 0.08 * S.back(c)})`;
    },
    type(el, text, x) {
      if (!el) return;
      el.textContent = text.slice(0, Math.round(clamp(x) * text.length));
    },
    num(x, from, to, decimals = 0, locale = "es-CO") {
      const v = from + (to - from) * clamp(x);
      return v.toLocaleString(locale, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });
    },
  };
  let fn = () => {};
  window.scene = (f) => {
    fn = f;
    window.__seek(0);
  };
  window.__seek = (t) => {
    fn(t, S);
    return true;
  };
  window.__S = S;
})();
