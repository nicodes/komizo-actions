'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {ordinaryDirectory} = require('./connect.cjs');
function cleanup(directory, parent) {
  if (!directory) return;
  const root = ordinaryDirectory(parent);
  if (path.dirname(directory) !== root || !/^komizo-submit-[a-zA-Z0-9]+$/.test(path.basename(directory))) throw new Error('Foreign transport directory');
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('Foreign transport identity');
  fs.rmSync(directory, {recursive: true, force: true});
}
if (require.main === module) {
  try { cleanup(process.env.STATE_directory, process.env.RUNNER_TEMP); }
  catch { process.stderr.write('Komizo private transport cleanup refused a foreign path.\n'); process.exitCode = 1; }
}
module.exports = {cleanup};
