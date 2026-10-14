'use strict';
// Scans transcripts off the main thread. Each 'scan' message re-reads changed files, saves the
// cache and replies with a summary.
const fs = require('fs');
const { parentPort, workerData } = require('worker_threads');
const { scan, summarize } = require('./data');

let cache = {};
try { cache = JSON.parse(fs.readFileSync(workerData.cachePath, 'utf8')); } catch { /* first run */ }
let busy = false;

parentPort.on('message', async () => {
  if (busy) return;
  busy = true;
  try {
    cache = await scan(cache);
    fs.writeFileSync(workerData.cachePath, JSON.stringify(cache));
    parentPort.postMessage({ ok: true, summary: summarize(cache) });
  } catch (e) {
    parentPort.postMessage({ ok: false, error: e.message });
  } finally {
    busy = false;
  }
});
