'use strict';

// Private child-process IPC only. Importing this file never connects to hardware.
const fs = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline');
const { Atem, Enums, Util } = require('atem-connection');
// Pinned to atem-connection 3.10.2: this helper exposes the same conversion/hash
// used by uploadStill, allowing confirmation against the switcher's MPfe reply.
const { generateUploadBufferInfo } = require('atem-connection/dist/dataTransfer/dataTransferUploadBuffer');

function playerKey(player) {
    return JSON.stringify([player && player.sourceType, player && player.stillIndex, player && player.clipIndex]);
}

function videoModeName(modeId) {
    return String(Enums.VideoMode[modeId] || modeId).replace(/^[NP]/, '')
        .replace('4KHD', '2160').replace('8KHD', '4320')
        .replace(/([ip])(2398|2997|5994|5000)(?=$|NTSC|169)/,
            (_, scan, rate) => scan + ({2398: '23.98', 2997: '29.97', 5994: '59.94', 5000: '50'}[rate]))
        .replace(/169$/, ' (16:9)').replace(/(NTSC|PAL)$/, ' $1');
}

class MediaWorker {
    constructor(atem, emit, options = {}) {
        this.atem = atem;
        this.emit = emit;
        this.convert = options.convert || generateUploadBufferInfo;
        this.readFrame = options.readFrame || (async (framePath, expectedLength) => {
            if (!path.isAbsolute(framePath) || !/^tdeck-atem-frame-.+\.rgba$/.test(path.basename(framePath))) {
                throw new Error('Invalid prepared image path');
            }
            const info = await fs.stat(framePath);
            if (!info.isFile() || info.size !== expectedLength) throw new Error('Prepared image has an incorrect size');
            return fs.readFile(framePath);
        });
        this.connected = false;
        this.generation = 0;
        this.revision = 0;
        this.error = '';
        this.active = null;
        this.broken = false;
        this.initialized = false;
        this.stateTimer = null;
        atem.on('connected', () => {
            this.connected = true;
            this.generation += 1;
            this.revision += 1;
            this.error = '';
            this.checkActive();
            this.publish();
        });
        atem.on('disconnected', () => {
            this.connected = false;
            this.generation += 1;
            this.revision += 1;
            this.error = 'ATEM media connection was lost';
            this.checkActive();
            this.publish();
        });
        atem.on('error', error => {
            this.revision += 1;
            this.error = String(error && error.message || error).slice(0, 500);
            if (this.active) this.active.abort(new Error(this.error));
            this.publish();
        });
        atem.on('stateChanged', () => {
            this.revision += 1;
            this.checkActive();
            if (!this.stateTimer) {
                this.stateTimer = setTimeout(() => { this.stateTimer = null; this.publish(); }, 200);
                this.stateTimer.unref();
            }
        });
    }

    snapshot() {
        const state = this.atem.state;
        const info = state && state.info || {};
        const modeId = state && state.settings && state.settings.videoMode;
        const mode = modeId === undefined ? undefined : Util.getVideoModeInfo(modeId);
        const playerCount = Number(info.capabilities && info.capabilities.mediaPlayers) || 0;
        const stillCount = Number(info.mediaPool && info.mediaPool.stillCount) || 0;
        const media = state && state.media || {};
        const players = Array.from({ length: playerCount }, (_, index) => {
            const player = (media.players || [])[index];
            const type = player && player.sourceType === Enums.MediaSourceType.Still ? 'still'
                : player && player.sourceType === Enums.MediaSourceType.Clip ? 'clip' : 'unknown';
            return { player: index + 1, type,
                slot: player ? (type === 'clip' ? player.clipIndex : player.stillIndex) + 1 : null,
                stillSlot: player && Number.isInteger(player.stillIndex) ? player.stillIndex + 1 : null };
        });
        const stills = Array.from({ length: stillCount }, (_, index) => {
            const still = (media.stillPool || [])[index];
            return { slot: index + 1, used: !!(still && still.isUsed), name: still && still.fileName || '', known: !!still };
        });
        const connected = this.connected && !this.broken;
        return { connected, ready: !!(connected && mode && playerCount && stillCount
                && players.every(player => player.type !== 'unknown' && Number.isInteger(player.stillSlot))),
            product: info.productIdentifier || '',
            videoMode: mode ? { id: modeId, name: videoModeName(modeId), width: mode.width, height: mode.height } : null,
            capabilities: { players: playerCount, stills: stillCount }, players, stills,
            generation: this.generation, revision: this.revision, error: this.error };
    }

    publish() {
        this.emit({ event: 'state', state: this.snapshot() });
    }

    async init(message) {
        if (this.initialized) throw new Error('ATEM media worker is already initialized');
        this.initialized = true;
        if (typeof message.host !== 'string' || !message.host.trim()
            || !Number.isInteger(message.port) || message.port < 1 || message.port > 65535) {
            throw new Error('Invalid ATEM address or port');
        }
        await this.atem.connect(message.host, message.port);
        this.publish();
        return this.snapshot();
    }

    assertActive(active) {
        const snapshot = this.snapshot();
        if (!snapshot.ready || this.generation !== active.generation) {
            throw new Error('ATEM connection changed during the image load');
        }
        if (snapshot.videoMode.id !== active.videoModeId || snapshot.videoMode.width !== active.width
            || snapshot.videoMode.height !== active.height) throw new Error('ATEM video format changed during the image load');
        const player = this.atem.state.media.players[active.player - 1];
        const desired = player && player.sourceType === Enums.MediaSourceType.Still && player.stillIndex === active.slot - 1;
        if (playerKey(player) !== active.originalPlayer && !(active.phase === 'selecting' && desired)) {
            throw new Error('The destination media player was changed by another operator');
        }
        // Protect even a clip player's retained still selection: it may be put
        // back on air while an upload is in progress.
        if (active.phase !== 'selecting' && this.atem.state.media.players.some(player => player && player.stillIndex === active.slot - 1)) {
            throw new Error('The reserved still slot is selected by a media player');
        }
    }

    checkActive() {
        if (!this.active) return;
        try { this.assertActive(this.active); }
        catch (error) { this.active.abort(error); }
    }

    async waitFor(active, condition, description) {
        while (true) {
            this.assertActive(active);
            if (condition()) return;
            if (Date.now() >= active.deadline) throw new Error(description);
            await Promise.race([new Promise(resolve => setTimeout(resolve, 25)), active.aborted]);
        }
    }

    async load(message) {
        if (this.active) throw new Error('Another media transfer is running');
        const state = this.snapshot();
        if (!state.ready) throw new Error('ATEM media is not ready');
        if (!Number.isInteger(message.player) || message.player < 1 || message.player > state.capabilities.players) {
            throw new Error('Configured media player is unavailable');
        }
        if (!Array.isArray(message.allowedSlots) || message.allowedSlots.length < 2
            || new Set(message.allowedSlots).size !== message.allowedSlots.length
            || message.allowedSlots.some(slot => !Number.isInteger(slot) || slot < 1 || slot > state.capabilities.stills)) {
            throw new Error('Reserved still slots do not match this ATEM');
        }
        const usedSlots = new Set(state.players.map(player => player.stillSlot));
        const slot = message.allowedSlots.find(candidate => !usedSlots.has(candidate));
        if (!slot) throw new Error('Every reserved still slot is selected by a media player. Free a reserved slot first.');
        if (message.generation !== this.generation || message.videoModeId !== state.videoMode.id
            || message.width !== state.videoMode.width || message.height !== state.videoMode.height) {
            throw new Error('ATEM connection or video format changed while preparing the image');
        }
        const expected = message.expectedPlayer;
        const current = state.players[message.player - 1];
        if (!expected || expected.player !== current.player || expected.type !== current.type || expected.slot !== current.slot
            || (Number.isInteger(expected.stillSlot) && expected.stillSlot !== current.stillSlot)) {
            throw new Error('The destination media player changed while preparing the image');
        }
        let abort;
        const aborted = new Promise((_, reject) => { abort = reject; });
        // Always observe abort rejection, including synchronous preparation failures.
        aborted.catch(() => {});
        const active = { slot, player: message.player, generation: this.generation,
            videoModeId: state.videoMode.id, width: message.width, height: message.height,
            originalPlayer: playerKey(this.atem.state.media.players[message.player - 1]),
            phase: 'uploading', abort, aborted,
            deadline: Date.now() + Math.min(180000, Math.max(1, Number(message.timeoutMs) || 180000)) };
        this.active = active;
        const timer = setTimeout(() => abort(new Error('ATEM image load timed out; completion was not confirmed')),
            Math.max(1, active.deadline - Date.now()));
        const stage = status => this.emit({ event: 'stage', jobId: message.jobId, status, slot });
        try {
            const expectedLength = message.width * message.height * 4;
            const frame = await Promise.race([this.readFrame(message.framePath, expectedLength), aborted]);
            if (!Buffer.isBuffer(frame) || frame.length !== expectedLength) throw new Error('Prepared image has an incorrect size');
            this.assertActive(active);
            const encoded = this.convert(frame, { width: message.width, height: message.height }, true);
            this.assertActive(active);
            stage('uploading');
            // uploadStill resolves on FTDC transfer completion, not packet ACKs.
            await Promise.race([this.atem.uploadStill(slot - 1, encoded,
                String(message.name || 'TDeck image').slice(0, 64), 'TDeck image'), aborted]);
            this.assertActive(active);
            const matches = () => {
                const still = this.atem.state.media.stillPool[slot - 1];
                return !!(still && still.isUsed && still.hash === encoded.hash);
            };
            await this.waitFor(active, matches, 'ATEM did not confirm the uploaded image');
            this.assertActive(active);
            active.phase = 'selecting';
            stage('selecting');
            await Promise.race([this.atem.setMediaPlayerSource({ sourceType: Enums.MediaSourceType.Still,
                stillIndex: slot - 1 }, message.player - 1), aborted]);
            await this.waitFor(active, () => {
                const player = this.atem.state.media.players[message.player - 1];
                return matches() && player.sourceType === Enums.MediaSourceType.Still && player.stillIndex === slot - 1;
            }, 'ATEM did not confirm media player selection');
            this.assertActive(active);
            const result = { confirmed: true, slot, state: this.snapshot() };
            this.publish();
            return result;
        } catch (error) {
            // Stop outstanding transfer packets before any subsequent request can
            // reuse this connection. The parent will create a fresh worker later.
            this.broken = true;
            this.error = String(error.message || error).slice(0, 500);
            this.publish();
            Promise.resolve(this.atem.disconnect()).catch(() => {});
            throw error;
        } finally {
            clearTimeout(timer);
            this.active = null;
        }
    }

    async close() {
        if (this.active) this.active.abort(new Error('ATEM media worker stopped'));
        if (this.stateTimer) clearTimeout(this.stateTimer);
        await this.atem.destroy();
    }
}

function runIpc(atem, options = {}) {
    const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
    const worker = new MediaWorker(atem, emit, options);
    const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    input.on('line', line => {
        if (line.length > 65536) return;
        let message;
        try { message = JSON.parse(line); }
        catch (_) { return; }
        if (!message || typeof message.id !== 'string') return;
        (async () => {
            if (message.op === 'init') return worker.init(message);
            if (message.op === 'snapshot') return worker.snapshot();
            if (message.op === 'load') return worker.load(message);
            throw new Error('Unsupported ATEM media operation');
        })().then(result => emit({ id: message.id, result })).catch(error => {
            emit({ id: message.id, error: String(error.message || error).slice(0, 500) });
            if (worker.broken) setTimeout(() => process.exit(1), 20);
        });
    });
    input.on('close', () => {
        const fallback = setTimeout(() => process.exit(0), 500);
        worker.close().finally(() => { clearTimeout(fallback); process.exit(0); });
    });
    return worker;
}

module.exports = { MediaWorker, runIpc };
if (require.main === module) runIpc(new Atem({ disableMultithreaded: true }));
