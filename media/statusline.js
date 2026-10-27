#!/usr/bin/env node
// Claude Code statusLine command for AI Status: saves the plan usage limits Claude Code passes on stdin
// (rate_limits.five_hour / seven_day: used_percentage, resets_at) to <config>/ai-status-usage.json and prints
// "5h 6% · 7d 27%". No token and no network: it only uses what Claude Code already sends to the status line.
//
// settings.json:  "statusLine": {"type": "command", "command": "node <path>/statusline.js"}
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");

const CONFIG = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
const OUT = path.join(CONFIG, "ai-status-usage.json");

let input = "";
process.stdin.setEncoding("utf8").on("data", (c) => (input += c)).on("end", () => {
  let data = {};
  try { data = JSON.parse(input || "{}"); } catch {}
  const limits = data.rate_limits || {};
  if (limits.five_hour || limits.seven_day) {
    const tmp = OUT.replace(/\.json$/, ".tmp");
    fs.writeFileSync(tmp, JSON.stringify({ updated: Date.now() / 1000, rate_limits: limits }));
    fs.renameSync(tmp, OUT);   // quem lê nunca vê o arquivo pela metade
  }
  const parts = [["five_hour", "5h"], ["seven_day", "7d"]]
    .filter(([key]) => (limits[key] || {}).used_percentage != null)
    .map(([key, label]) => `${label} ${Math.round(limits[key].used_percentage)}%`);
  process.stdout.write(parts.join(" · "));
});
