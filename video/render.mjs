// Render scenes frame by frame with headless Chromium.
//   node render.mjs s08                 -> build/scenes/s08.mp4 (30 fps, whole scene)
//   node render.mjs s08 --at 0,4.5,12   -> build/preview/s08_<t>.png stills (fast check)
//   node render.mjs all                 -> every scene in build/timeline.js
import { chromium } from "playwright-core";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FPS = 30;
const exe =
  process.env.CHROME ||
  path.join(os.homedir(), ".cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell");

function timeline() {
  const src = fs.readFileSync(path.join(HERE, "build/timeline.js"), "utf8");
  const ctx = { window: {} };
  new Function("window", src)(ctx.window);
  return ctx.window.TIMELINE;
}

async function open(browser, id) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  page.on("pageerror", (e) => console.error(`[${id}] pageerror: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && console.error(`[${id}] console: ${m.text()}`));
  await page.goto(pathToFileURL(path.join(HERE, "scenes", `${id}.html`)).href, { waitUntil: "load" });
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map((i) => (i.complete ? null : new Promise((r) => (i.onload = i.onerror = r)))));
  });
  return page;
}

async function stills(browser, id, times) {
  const page = await open(browser, id);
  fs.mkdirSync(path.join(HERE, "build/preview"), { recursive: true });
  for (const t of times) {
    await page.evaluate((t) => window.__seek(t), t);
    const f = path.join(HERE, "build/preview", `${id}_${t}.png`);
    await page.screenshot({ path: f, type: "png" });
    console.log(f);
  }
  await page.close();
}

async function video(browser, id, dur) {
  const page = await open(browser, id);
  fs.mkdirSync(path.join(HERE, "build/scenes"), { recursive: true });
  const out = path.join(HERE, "build/scenes", `${id}.mp4`);
  const ff = spawn(
    "ffmpeg",
    ["-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", String(FPS), "-c:v", "mjpeg", "-i", "-",
     "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-r", String(FPS), out],
    { stdio: ["pipe", "inherit", "inherit"] },
  );
  const n = Math.round(dur * FPS);
  for (let i = 0; i < n; i++) {
    await page.evaluate((t) => window.__seek(t), i / FPS);
    const buf = await page.screenshot({ type: "jpeg", quality: 94 });
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
  }
  ff.stdin.end();
  await new Promise((r) => ff.on("close", r));
  await page.close();
  console.log(`${out} (${n} frames)`);
}

const args = process.argv.slice(2);
const at = args.indexOf("--at");
const times = at >= 0 ? args[at + 1].split(",").map(Number) : null;
const TL = timeline();
let ids = args.filter((a, i) => !a.startsWith("--") && !(at >= 0 && i === at + 1));
if (ids.includes("all")) ids = Object.keys(TL.scenes);
const browser = await chromium.launch({ executablePath: exe, args: ["--allow-file-access-from-files", "--font-render-hinting=none"] });
const par = Number(process.env.PAR || 4);
const queue = [...ids];
await Promise.all(
  Array.from({ length: Math.min(par, queue.length) }, async () => {
    while (queue.length) {
      const id = queue.shift();
      if (times) await stills(browser, id, times);
      else await video(browser, id, TL.scenes[id].duration);
    }
  }),
);
await browser.close();
