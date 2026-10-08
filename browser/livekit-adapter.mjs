import * as livekit from './vendor/livekit-client.esm.mjs';

export function createLiveKitAdapter({ sdk = livekit, room = new sdk.Room(), audioContext,
  hostIdentity, onNotification, onTrack, onDisconnect }) {
  const microphones = new Set(), outputs = new Set(), handlers = [];
  let closed = false, disconnecting = null;
  const hostWaits = new Set();
  function listen(event, callback) {
    room.on(event, callback);
    handlers.push([event, callback]);
  }
  listen(sdk.RoomEvent.DataReceived, (payload, participant, _kind, topic) => {
    if (!participant || participant.identity !== hostIdentity ||
        topic !== 'local-gpt-live.events.v1' || closed) return;
    let message;
    try { message = JSON.parse(new TextDecoder().decode(payload)); }
    catch { return; }
    onNotification(message, { identity: participant.identity, sid: participant.sid, topic });
  });
  listen(sdk.RoomEvent.TrackSubscribed, (track, publication, participant) => {
    if (!closed && track.kind === sdk.Track.Kind.Audio) onTrack({
      track, trackSid: publication.trackSid,
      participantIdentity: participant.identity, participantSid: participant.sid,
    });
  });
  listen(sdk.RoomEvent.TrackUnsubscribed, (_track, publication, participant) => {
    if (closed) return;
    onTrack({ removed: true, trackSid: publication.trackSid,
      participantIdentity: participant.identity, participantSid: participant.sid });
  });
  const lost = () => { if (!closed) onDisconnect(); };
  listen(sdk.RoomEvent.Disconnected, lost);
  listen(sdk.RoomEvent.Reconnecting, lost);
  listen(sdk.RoomEvent.ParticipantDisconnected, participant => {
    if (participant.identity === hostIdentity) lost();
  });
  listen(sdk.RoomEvent.ParticipantConnected, () => {
    for (const check of [...hostWaits]) check();
  });
  function waitForHost(hostSid) {
    return new Promise((resolve, reject) => {
      const finish = error => {
        clearTimeout(timer); hostWaits.delete(check);
        if (error) reject(error); else resolve();
      };
      const check = () => {
        if (closed) return finish(new Error('disconnected'));
        const participant = room.remoteParticipants.get(hostIdentity);
        if (!participant) return;
        if (participant.identity !== hostIdentity || participant.sid !== hostSid) {
          finish(new Error('host_identity_mismatch'));
        } else finish();
      };
      const timer = setTimeout(() => finish(new Error('rpc_timeout')), 5000);
      hostWaits.add(check); check();
    });
  }
  async function invoke(method, message, timeoutMs) {
    if (closed) throw new Error('disconnected');
    try {
      return JSON.parse(await room.localParticipant.performRpc({
        destinationIdentity: hostIdentity, method,
        payload: JSON.stringify(message), responseTimeout: timeoutMs,
      }));
    } catch (error) {
      if ([1501, 1502].includes(error.code)) throw new Error('rpc_timeout');
      throw new Error('rpc_failed');
    }
  }
  async function microphone() {
    const track = await sdk.createLocalAudioTrack({ echoCancellation: true, noiseSuppression: true });
    if (closed) { track.stop(); throw new Error('operation_invalidated'); }
    let publication;
    try {
      publication = await room.localParticipant.publishTrack(track, {
        source: sdk.Track.Source.Microphone, dtx: false,
      });
    } catch (error) { track.stop(); throw error; }
    let closing = null;
    const resource = {
      trackSid: publication.trackSid,
      suppress() { track.mediaStreamTrack.enabled = false; },
      close() {
        if (!closing) closing = (async () => {
          resource.suppress();
          try { await room.localParticipant.unpublishTrack(track, false); }
          finally { track.stop(); microphones.delete(resource); }
        })();
        return closing;
      },
    };
    microphones.add(resource);
    if (closed) { await resource.close(); throw new Error('operation_invalidated'); }
    return resource;
  }
  async function prepareOutput(track, onFailure) {
    if (closed || track.mediaStreamTrack?.kind !== 'audio' ||
        track.mediaStreamTrack.readyState !== 'live') throw new Error('output_not_ready');
    await audioContext.resume();
    if (closed || audioContext.state !== 'running' ||
        track.mediaStreamTrack.readyState !== 'live') throw new Error('output_not_ready');
    const source = audioContext.createMediaStreamSource(new MediaStream([track.mediaStreamTrack]));
    source.connect(audioContext.destination);
    // SDKのRemoteAudioTrackと同様に、消音要素でWebRTC decoderを駆動する。
    const decoder = document.createElement('audio');
    decoder.muted = true;
    decoder.volume = 0;
    decoder.srcObject = new MediaStream([track.mediaStreamTrack]);
    let released = false;
    const output = { close() {
      if (!released) {
        released = true; decoder.onerror = null; decoder.pause(); decoder.srcObject = null;
        source.disconnect(); outputs.delete(output);
      }
    } };
    outputs.add(output);
    let failure = null;
    const failed = cause => {
      if (released) return;
      failure = cause;
      output.close(); onFailure(cause);
    };
    decoder.onerror = () => failed(new Error('output_not_ready'));
    try { decoder.play().catch(failed); }
    catch (cause) { failed(cause); }
    // 無音RTPではplay完了がPCM到着を待つため、そのPromiseはready条件にしない。
    await Promise.resolve();
    if (failure) throw failure;
    if (closed) { output.close(); throw new Error('output_not_ready'); }
    return output;
  }
  return {
    async connect(url, token) {
      await room.connect(url, token);
      return { participantIdentity: room.localParticipant.identity, participantSid: room.localParticipant.sid };
    },
    async requestState(connection) {
      if (connection.hostIdentity !== hostIdentity || !connection.hostSid) {
        throw new Error('host_identity_mismatch');
      }
      await waitForHost(connection.hostSid);
      const participant = room.remoteParticipants.get(hostIdentity);
      if (closed || participant?.identity !== hostIdentity || participant.sid !== connection.hostSid) {
        throw new Error('host_identity_mismatch');
      }
      const message = await invoke('local-gpt-live.state.v1', {
        v: 1, session_id: connection.sessionId, connection_id: connection.connectionId,
      }, 5000);
      const current = room.remoteParticipants.get(hostIdentity);
      if (closed || current !== participant || current?.identity !== hostIdentity ||
          current.sid !== connection.hostSid ||
          !message?.ok || message.v !== 1 || message.type !== 'host_state' ||
          message.session_id !== connection.sessionId || message.connection_id !== connection.connectionId) {
        throw new Error('invalid_state');
      }
      return { message, sender: { identity: current.identity, sid: current.sid,
        topic: 'local-gpt-live.events.v1' } };
    },
    rpc: (message, timeoutMs) => invoke('local-gpt-live.control.v1', message, timeoutMs),
    microphone, prepareOutput,
    disconnect() {
      if (disconnecting) return disconnecting;
      closed = true;
      for (const check of [...hostWaits]) check();
      for (const [event, callback] of handlers) room.off(event, callback);
      for (const output of [...outputs]) output.close();
      disconnecting = (async () => {
        try { await Promise.all([...microphones].map(mic => mic.close())); }
        finally { await room.disconnect(); }
      })();
      return disconnecting;
    },
  };
}
