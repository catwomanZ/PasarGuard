/* Fuzz the JS Jalali conversion against Python's jdatetime.
   Reads JSON lines: {"y":..,"m":..,"d":..,"jy":..,"jm":..,"jd":..} on stdin,
   exits non-zero on the first disagreement. */
const J = require('/home/user/pgship/templates/subscription/jalali.js');
const lines = require('fs').readFileSync(0, 'utf8').trim().split('\n');
let n = 0, bad = 0;
for (const ln of lines) {
  if (!ln.trim()) continue;
  const r = JSON.parse(ln);
  const got = J.g2j(r.y, r.m, r.d);
  n++;
  if (got.jy !== r.jy || got.jm !== r.jm || got.jd !== r.jd) {
    bad++;
    if (bad <= 8) console.log(`  MISMATCH ${r.y}-${r.m}-${r.d}: py=${r.jy}/${r.jm}/${r.jd} js=${got.jy}/${got.jm}/${got.jd}`);
  }
}
console.log(`  checked ${n} dates, mismatches: ${bad}`);
process.exit(bad ? 1 : 0);
