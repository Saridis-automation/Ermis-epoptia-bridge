'use strict';
const el = id => document.getElementById(id);
let active = null;

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body), signal: AbortSignal.timeout(45000)
  });
  const data = await response.json();
  if (!response.ok) throw new Error('Gateway request failed');
  return data;
}

function send(state, event) {
  if (active === state && state.channel?.readyState === 'open') {
    state.channel.send(JSON.stringify(event));
  }
}

function output(state, callId, result) {
  send(state, {type: 'conversation.item.create', item: {
    type: 'function_call_output', call_id: callId, output: JSON.stringify(result)
  }});
}

function showProposal(state) {
  const proposal = state.proposals[0];
  el('approval').hidden = !proposal;
  if (proposal) el('proposal').textContent = proposal.result.prompt;
}

async function handleResponse(state, event) {
  // response.done includes complete function arguments and all parallel calls.
  if (event.type !== 'response.done' || event.response?.status !== 'completed') return;
  let handled = false;
  for (const item of event.response?.output || []) {
    if (item.type !== 'function_call' || state.seen.has(item.call_id)) continue;
    state.seen.add(item.call_id);
    handled = true;
    let result;
    try {
      result = await post('/voice/tool', {session_id: state.sid, call_id: item.call_id,
        name: item.name, arguments: JSON.parse(item.arguments)});
    } catch (_) {
      result = {ok: false, status: 'tool_unavailable'};
    }
    if (active !== state) return;
    if (result.status === 'confirmation_required') {
      state.proposals.push({result});
      showProposal(state);
      // The model never receives a confirmation token or an approval function.
      output(state, item.call_id, {ok: true, status: 'confirmation_required',
        action: result.action, arguments: result.arguments, prompt: result.prompt});
    } else {
      output(state, item.call_id, result);
    }
  }
  if (handled) send(state, {type: 'response.create'});
}

async function decide(approved) {
  const state = active;
  if (!state || state.deciding || !state.proposals.length) return;
  state.deciding = true;
  el('approve').disabled = el('cancel').disabled = true;
  const proposal = state.proposals.shift();
  try {
    const result = await post('/voice/confirm', {session_id: state.sid,
      confirmation_id: proposal.result.confirmation_id, approved});
    if (active !== state) return;
    el('status').textContent = `Action: ${result.status}`;
    send(state, {type: 'conversation.item.create', item: {type: 'message', role: 'user',
      content: [{type: 'input_text', text: 'Gateway confirmation result: ' + JSON.stringify(result)}]}});
    send(state, {type: 'response.create'});
  } catch (_) {
    if (active === state) el('status').textContent = 'Confirmation outcome unknown. Check service status; do not retry blindly.';
  } finally {
    state.deciding = false;
    if (active === state) {
      el('approve').disabled = el('cancel').disabled = false;
      showProposal(state);
    }
  }
}

async function stop(message = 'Stopped') {
  const state = active;
  active = null;
  if (state) {
    clearTimeout(state.timer);
    state.stream?.getTracks().forEach(track => track.stop());
    state.channel?.close();
    state.peer?.close();
  }
  el('audio').srcObject = null;
  el('approval').hidden = true;
  el('start').disabled = false;
  el('stop').disabled = true;
  el('status').textContent = message;
  if (state?.sid) {
    try {
      const result = await post('/voice/stop', {session_id: state.sid});
      if (!result.ok && !active) el('status').textContent = 'Microphone stopped; server hangup unverified.';
    } catch (_) {
      if (!active) el('status').textContent = 'Microphone stopped; server hangup unverified.';
    }
  }
}

async function start() {
  if (active) return;
  const state = {proposals: [], seen: new Set(), deciding: false};
  active = state;
  el('start').disabled = true;
  el('stop').disabled = false;
  el('approve').disabled = el('cancel').disabled = false;
  el('status').textContent = 'Connecting…';
  // Includes microphone permission and SDP negotiation; Stop can cancel either.
  state.timer = setTimeout(() => { if (active === state) void stop('Connection timed out'); }, 60000);
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    if (active !== state) { stream.getTracks().forEach(track => track.stop()); return; }
    state.stream = stream;
    state.peer = new RTCPeerConnection();
    state.peer.ontrack = event => {
      if (active === state) {
        el('audio').srcObject = event.streams[0];
        el('audio').play().catch(() => {
          if (active === state) el('status').textContent = 'Press audio play to hear Ermis.';
        });
      }
    };
    state.peer.onconnectionstatechange = () => {
      if (active === state && ['failed', 'disconnected', 'closed'].includes(state.peer.connectionState)) {
        void stop('Connection ended');
      }
    };
    stream.getTracks().forEach(track => state.peer.addTrack(track, stream));
    state.channel = state.peer.createDataChannel('oai-events');
    state.channel.onopen = () => { if (active === state) el('status').textContent = 'Listening — ask a production question'; };
    state.channel.onclose = () => { if (active === state) void stop('Connection ended'); };
    let queue = Promise.resolve();
    state.channel.onmessage = event => {
      queue = queue.then(async () => {
        if (active !== state) return;
        const data = JSON.parse(event.data);
        if (data.type === 'error') el('status').textContent = 'Voice error. Stop and start a new conversation.';
        await handleResponse(state, data);
      }).catch(() => { if (active === state) el('status').textContent = 'Voice event could not be handled.'; });
    };
    const offer = await state.peer.createOffer();
    if (active !== state) return;
    await state.peer.setLocalDescription(offer);
    if (active !== state) return;
    const result = await post('/voice/session', {sdp: state.peer.localDescription.sdp});
    state.sid = result.session_id;
    if (active !== state) { await post('/voice/stop', {session_id: state.sid}); return; }
    await state.peer.setRemoteDescription({type: 'answer', sdp: result.sdp});
    if (active !== state) return;
    clearTimeout(state.timer);
    state.timer = setTimeout(() => { if (active === state) void stop('Conversation time limit reached'); },
      result.expires_in_seconds * 1000);
  } catch (_) {
    if (active === state) await stop('Unable to connect. Check microphone permission and gateway configuration.');
  }
}

el('start').onclick = start;
el('stop').onclick = () => stop();
el('approve').onclick = () => decide(true);
el('cancel').onclick = () => decide(false);
window.addEventListener('pagehide', () => {
  const state = active;
  // Clear ownership before closing channels (including back/forward cache exits).
  const sid = state?.sid;
  if (state) state.sid = null;
  void stop();
  if (sid) {
    try {
      navigator.sendBeacon('/voice/stop', new Blob([JSON.stringify({session_id: sid})],
        {type: 'application/json'}));
    } catch (_) { /* Local cleanup has already completed; server TTL remains. */ }
  }
});
