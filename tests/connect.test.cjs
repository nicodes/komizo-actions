'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {prepare} = require('../connect/connect.cjs');
const {cleanup} = require('../connect/cleanup.cjs');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'komizo-transport-test-'));
try {
  const key = path.join(temporary, 'host');
  assert.equal(spawnSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-f', key]).status, 0);
  const environment = {RUNNER_TEMP: temporary, INPUT_HOST: 'host.example', INPUT_KEY: fs.readFileSync(key, 'utf8'),
                       'INPUT_KNOWN-HOSTS': 'host.example ' + fs.readFileSync(key + '.pub', 'utf8')};
  const result = prepare(environment);
  const config = fs.readFileSync(result.configuration, 'utf8');
  for (const directive of ['User komizo-submit', 'StrictHostKeyChecking yes', 'IdentityAgent none',
                            'ControlMaster no', 'ControlPath none', 'ForwardAgent no', 'ProxyCommand none']) assert.ok(config.includes(directive));
  for (const filename of ['config', 'identity', 'known_hosts']) assert.equal(fs.statSync(path.join(result.directory, filename)).mode & 0o777, 0o600);
  assert.equal(fs.statSync(result.directory).mode & 0o777, 0o700);
  assert.throws(() => cleanup(temporary, path.dirname(temporary)));
  for (const changes of [{INPUT_HOST: 'host\nProxyCommand bad'}, {INPUT_PORT: '65536'},
                         {'INPUT_KNOWN-HOSTS': 'another.example ' + fs.readFileSync(key + '.pub', 'utf8')},
                         {INPUT_KEY: '\0key'}]) assert.throws(() => prepare({...environment, ...changes}));
  cleanup(result.directory, temporary);
  assert.ok(!fs.existsSync(result.directory));
} finally { fs.rmSync(temporary, {recursive: true, force: true}); }
