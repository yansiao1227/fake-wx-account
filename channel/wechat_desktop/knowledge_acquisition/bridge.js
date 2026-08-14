"use strict";

const path = require("path");

function readStdin() {
  return new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { input += chunk; });
    process.stdin.on("end", () => resolve(input));
    process.stdin.on("error", reject);
  });
}

async function run() {
  const skillDir = path.resolve(process.argv[2] || "");
  const originalLog = console.log;
  console.log = (...args) => process.stderr.write(`${args.join(" ")}\n`);
  console.warn = (...args) => process.stderr.write(`${args.join(" ")}\n`);
  console.error = (...args) => process.stderr.write(`${args.join(" ")}\n`);

  const input = JSON.parse(await readStdin());
  const url = String(input.url || "").trim();
  if (!/^https?:\/\//i.test(url)) throw new Error("invalid URL");

  const { extractorInstance } = require(
    path.join(skillDir, "lib", "dynamic-content-extractor.js")
  );
  const result = await extractorInstance.extractContent(url);
  console.log = originalLog;
  process.stdout.write(JSON.stringify(result || {}), () => process.exit(0));
}

run().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
