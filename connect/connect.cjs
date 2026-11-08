'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

function ordinaryDirectory(directory) {
  const absolute = path.resolve(directory);
  for (let current = absolute;; current = path.dirname(current)) {
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Transport parent must be an ordinary directory');
    if (path.dirname(current) === current) break;
  }
  return absolute;
}
function prepare(environment) {
  const host = environment.INPUT_HOST || environment.KOMIZO_SERVER_URL || '';
  const port = environment.INPUT_PORT || '22';
  const key = environment.INPUT_KEY || environment.KOMIZO_DEPLOY_KEY || '';
  const pins = environment['INPUT_KNOWN-HOSTS'] || environment.KOMIZO_KNOWN_HOSTS || '';
  if (!/^[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}$/.test(host) || host.includes('..') ||
      !/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535 ||
      !key || Buffer.byteLength(key) > 65536 || key.includes('\0') ||
      !pins || Buffer.byteLength(pins) > 65536 || pins.includes('\0')) throw new Error('A bounded host, private key and verified host pins are required');
  const parent = ordinaryDirectory(environment.RUNNER_TEMP || os.tmpdir());
  if (/[\r\n\0"%\\]/.test(parent)) throw new Error('Transport parent has unsupported characters');
  const directory = fs.mkdtempSync(path.join(parent, 'komizo-submit-'));
  fs.chmodSync(directory, 0o700);
  const configuration = path.join(directory, 'config');
  try {
    fs.writeFileSync(path.join(directory, 'identity'), key.replace(/\s*$/, '\n'), {mode: 0o600, flag: 'wx'});
    fs.writeFileSync(path.join(directory, 'known_hosts'), pins.replace(/\s*$/, '\n'), {mode: 0o600, flag: 'wx'});
    const lookup = port === '22' ? host : `[${host}]:${port}`;
    const checked = spawnSync('ssh-keygen', ['-F', lookup, '-f', path.join(directory, 'known_hosts')],
                              {stdio: 'ignore', timeout: 10000});
    if (checked.status !== 0) throw new Error('Host pins contain no exact host and port entry');
    const text = `Host deploy-target\n  HostName ${host}\n  Port ${port}\n  User komizo-submit\n  IdentityFile "${directory}/identity"\n  UserKnownHostsFile "${directory}/known_hosts"\n  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n  IdentityAgent none\n  BatchMode yes\n  ControlMaster no\n  ControlPath none\n  ForwardAgent no\n  ForwardX11 no\n  ClearAllForwardings yes\n  PermitLocalCommand no\n  ProxyCommand none\n  ProxyJump none\n  RequestTTY no\n`;
    fs.writeFileSync(configuration, text, {mode: 0o600, flag: 'wx'});
    return {directory, configuration};
  } catch (error) {
    fs.rmSync(directory, {recursive: true, force: true});
    throw error;
  }
}
function main() {
  let prepared;
  try {
    prepared = prepare(process.env);
    const {directory, configuration} = prepared;
    fs.appendFileSync(process.env.GITHUB_STATE, `directory=${directory}\n`);
    fs.appendFileSync(process.env.GITHUB_ENV, `KOMIZO_SSH_CONFIG=${configuration}\n`);
    fs.appendFileSync(process.env.GITHUB_OUTPUT, `ssh-config=${configuration}\n`);
  } catch {
    if (prepared) fs.rmSync(prepared.directory, {recursive: true, force: true});
    process.stderr.write('Komizo transport preparation refused. Verify the host, private key and exact host pins.\n');
    process.exitCode = 1;
  }
}
module.exports = {prepare, ordinaryDirectory};
if (require.main === module) main();
