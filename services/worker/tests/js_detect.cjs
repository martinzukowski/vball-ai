// Runs the ORIGINAL detect.js on feature sets read from stdin (JSON array of
// {features, options}) and prints the results as JSON. Used by test_heuristic_parity.py.
const path = require("path");
const { detectRallies } = require(path.join(__dirname, "../../../reference/rally-cutter-js/detect.js"));

let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const cases = JSON.parse(input);
  const out = cases.map(({ features, options }) => {
    const r = detectRallies(features, options);
    return { rallies: r.rallies, breaks: r.breaks, whistles: r.whistles, serves: r.serves, mode: r.mode };
  });
  process.stdout.write(JSON.stringify(out));
});
