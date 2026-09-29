const fs = require('node:fs');
const path = require('node:path');
const {PrivateStore} = require('../epoptia_browser_session.cjs');
const {Scheduler} = require('../epoptia_browser_scheduler.cjs');
class FixtureStore extends PrivateStore {
  prepare() {
    // The checkout is group-writable; synthetic fixtures trust that boundary only.
    const stat = fs.lstatSync(this.directory);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o700) throw Error('unsafe_storage');
  }
}
async function scheduled(operation) {
  const dir = fs.mkdtempSync(path.join(__dirname, '.browser-policy-'));
  fs.chmodSync(dir, 0o700);
  let clock = 0;
  const store = new FixtureStore(dir);
  const scheduler = new Scheduler({store, now: () => clock, sleep: async ms => { clock += ms; }});
  try {
    let failure;
    const result = await scheduler.run(async s => {
      try { return await operation(s); } catch (error) { failure = error; throw error; }
    });
    if (failure) throw failure;
    return result;
  }
  finally {
    for (const file of fs.readdirSync(dir)) fs.unlinkSync(path.join(dir, file));
    fs.rmdirSync(dir);
  }
}
module.exports = {scheduled, FixtureStore};
