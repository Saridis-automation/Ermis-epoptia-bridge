'use strict';
// Test-only source loader. Never installed or imported by production modules.
// Overrides live only in this module instance, with a synthetic environment.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
function load(name, overrides = {}, env = {}) {
  const filename = path.resolve(__dirname, '..', name);
  const nativeRequire = createRequire(filename);
  const module = {exports: {}};
  const request = key => Object.hasOwn(overrides, key) ? overrides[key] : nativeRequire(key);
  const isolatedProcess = Object.create(process);
  isolatedProcess.env = env;
  vm.runInThisContext('(function(require,module,exports,__filename,__dirname,process){' +
    fs.readFileSync(filename, 'utf8') + '\n})', {filename})(request, module, module.exports,
    filename, path.dirname(filename), isolatedProcess);
  return module.exports;
}
class Backend {
  constructor({resolve = () => ({executablePath: '/SYNTHETIC'}), ...options} = {}) {
    const Real = load('epoptia_login_backend.cjs', {
      './epoptia_login_apparmor.cjs': {verify: resolve},
    }).Backend;
    return new Real(options);
  }
}
module.exports = {load, Backend};
