// Browser behavior with synthetic media, peer connection and HTTP only.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('gateway_static/voice.js', 'utf8');

function setup() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {disabled: false, hidden: true, textContent: '', play: async () => {}});
    return elements.get(id);
  };
  const calls = [], sent = [], timers = new Map(), listeners = new Map();
  let timerId = 0;
  const track = {stopped: false, stop() { this.stopped = true; }};
  const stream = {getTracks: () => [track]};
  const peers = [];
  class Peer {
    constructor() { peers.push(this); this.connectionState = 'new'; }
    addTrack() {}
    createDataChannel() {
      this.channel = {readyState: 'open', send: value => sent.push(JSON.parse(value)),
        close() { this.readyState = 'closed'; }};
      return this.channel;
    }
    async createOffer() { return {type: 'offer', sdp: 'v=0\r\nsynthetic'}; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) { this.remoteDescription = value; }
    close() { this.connectionState = 'closed'; }
  }
  const context = vm.createContext({
    document: {getElementById: element},
    window: {addEventListener(name, callback) { listeners.set(name, callback); }},
    navigator: {mediaDevices: {getUserMedia: async () => stream}},
    RTCPeerConnection: Peer, AbortSignal: {timeout() {}},
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (path, options) => {
      calls.push({path, body: JSON.parse(options.body)});
      return {ok: true, json: async () => path === '/voice/session'
        ? {ok: true, sdp: 'v=0\r\nanswer', session_id: 'synthetic_session', expires_in_seconds: 600}
        : {ok: true, status: 'completed'}};
    }
  });
  vm.runInContext(source, context);
  return {context, element, calls, sent, peers, track, stream, timers, listeners,
    run: code => vm.runInContext(code, context)};
}

test('start negotiates SDP, stop immediately releases microphone and peer', async () => {
  const h = setup();
  await h.element('start').onclick();
  assert.equal(h.calls[0].path, '/voice/session');
  assert.equal(h.peers[0].remoteDescription.type, 'answer');
  await h.element('stop').onclick();
  assert.equal(h.track.stopped, true);
  assert.equal(h.peers[0].connectionState, 'closed');
  assert.equal(h.calls.at(-1).path, '/voice/stop');
  assert.equal(h.timers.size, 0);
});

test('stop during microphone permission releases late stream without creating session', async () => {
  const h = setup();
  let grant;
  h.context.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { grant = resolve; });
  const starting = h.element('start').onclick();
  await h.element('stop').onclick();
  grant(h.stream);
  await starting;
  assert.equal(h.track.stopped, true);
  assert.equal(h.calls.length, 0);
});

test('failed session negotiation closes microphone and displays a safe error', async () => {
  const h = setup();
  h.context.fetch = async () => { throw new Error('synthetic private error'); };
  await h.element('start').onclick();
  assert.equal(h.track.stopped, true);
  assert.match(h.element('status').textContent, /Unable to connect/);
  assert.equal(h.element('status').textContent.includes('synthetic private'), false);
});

test('tools are relayed once and confirmation is sent only by approval button', async () => {
  const h = setup();
  await h.element('start').onclick();
  h.context.fetch = async (path, options) => {
    h.calls.push({path, body: JSON.parse(options.body)});
    return {ok: true, json: async () => path === '/voice/tool'
      ? {ok: true, status: 'confirmation_required', confirmation_id: 'synthetic-approval',
        prompt: 'Restart service ermis-system-mcp.service?', action: 'restart_service',
        arguments: {service: 'ermis-system-mcp.service'}}
      : {ok: true, status: 'completed'}};
  };
  const event = {type: 'response.done', response: {status: 'completed', output: [{type: 'function_call',
    call_id: 'call_synthetic', name: 'restart_service', arguments: JSON.stringify({service: 'ermis-system-mcp.service'})}]}};
  h.context.syntheticEvent = event;
  await h.run('handleResponse(active, syntheticEvent)');
  await h.run('handleResponse(active, syntheticEvent)');
  assert.equal(h.calls.filter(c => c.path === '/voice/tool').length, 1);
  assert.equal(h.calls.filter(c => c.path === '/voice/confirm').length, 0);
  assert.equal(h.element('approval').hidden, false);
  assert.equal(JSON.stringify(h.sent).includes('synthetic-approval'), false);
  await h.element('approve').onclick();
  assert.equal(h.calls.at(-1).path, '/voice/confirm');
  assert.equal(h.calls.at(-1).body.approved, true);
  assert.equal(h.element('approval').hidden, true);
});

test('late signaling after stop is hung up without reconnecting', async () => {
  const h = setup();
  let answer;
  h.context.fetch = async (path, options) => {
    h.calls.push({path, body: JSON.parse(options.body)});
    if (path === '/voice/session') return new Promise(resolve => { answer = resolve; });
    return {ok: true, json: async () => ({ok: true})};
  };
  const starting = h.element('start').onclick();
  while (!answer) await new Promise(resolve => setImmediate(resolve));
  await h.element('stop').onclick();
  answer({ok: true, json: async () => ({session_id: 'late_session', sdp: 'v=0', expires_in_seconds: 600})});
  await starting;
  assert.equal(h.calls.at(-1).path, '/voice/stop');
  assert.equal(h.peers[0].remoteDescription, undefined);
  assert.equal(h.track.stopped, true);
});

for (const phase of ['createOffer', 'setLocalDescription', 'setRemoteDescription']) {
  test(`stop during ${phase} prevents late work and timers`, async () => {
    const h = setup();
    let finish;
    h.context.RTCPeerConnection.prototype[phase] = function () {
      return new Promise(resolve => { finish = resolve; });
    };
    const starting = h.element('start').onclick();
    while (!finish) await new Promise(resolve => setImmediate(resolve));
    await h.element('stop').onclick();
    finish({type: 'offer', sdp: 'v=0\r\nsynthetic'});
    await starting;
    assert.equal(h.track.stopped, true);
    assert.equal(h.timers.size, 0);
    assert.equal(h.calls.filter(c => c.path === '/voice/session').length,
      phase === 'setRemoteDescription' ? 1 : 0);
  });
}

test('pagehide clears ownership and media even when beacon fails', async () => {
  const h = setup();
  await h.element('start').onclick();
  h.context.Blob = Blob;
  h.context.navigator.sendBeacon = () => { throw new Error('synthetic failure'); };
  h.listeners.get('pagehide')();
  assert.equal(h.run('active'), null);
  assert.equal(h.track.stopped, true);
  assert.equal(h.peers[0].channel.readyState, 'closed');
  assert.equal(h.timers.size, 0);
  assert.equal(h.element('start').disabled, false);
});

test('pagehide while signaling hangs up a late session', async () => {
  const h = setup();
  let answer;
  h.context.fetch = async (path, options) => {
    h.calls.push({path, body: JSON.parse(options.body)});
    if (path === '/voice/session') return new Promise(resolve => { answer = resolve; });
    return {ok: true, json: async () => ({ok: true})};
  };
  const starting = h.element('start').onclick();
  while (!answer) await new Promise(resolve => setImmediate(resolve));
  h.listeners.get('pagehide')();
  answer({ok: true, json: async () => ({session_id: 'late_session', sdp: 'v=0', expires_in_seconds: 600})});
  await starting;
  assert.equal(h.calls.at(-1).path, '/voice/stop');
  assert.equal(h.peers[0].remoteDescription, undefined);
  assert.equal(h.timers.size, 0);
});

test('cancelled or incomplete responses never dispatch tools', async () => {
  const h = setup();
  await h.element('start').onclick();
  for (const status of ['cancelled', 'failed', 'incomplete']) {
    h.context.syntheticEvent = {type: 'response.done', response: {status, output: [
      {type: 'function_call', call_id: 'call_partial', name: 'restart_service', arguments: '{}'}]}};
    await h.run('handleResponse(active, syntheticEvent)');
  }
  assert.equal(h.calls.filter(c => c.path === '/voice/tool').length, 0);
});
