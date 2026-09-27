import { gcm } from '@noble/ciphers/aes';
import { hkdf } from '@noble/hashes/hkdf';
import { sha256 } from '@noble/hashes/sha256';
import { bytesToUtf8, utf8ToBytes } from '@noble/hashes/utils';

const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const pairFormat = /^[A-Za-z0-9_-]{20,128}$/;
const maxMessages = 10000;

export interface Pairing {
  relay: string;
  device: string;
  pair: string;
  secret: string;
}

export interface Envelope {
  message_type: 'ciphertext';
  pair_id: string;
  nonce: string;
  ciphertext: string;
}

function validateRelayUrl(relay: string): void {
  if (!relay || relay !== relay.trim() || /[\u0000-\u001f]/.test(relay)) {
    throw new Error('Invalid relay URL');
  }
  let url: URL;
  try {
    url = new URL(relay);
  } catch {
    throw new Error('Invalid relay URL');
  }
  const authority = relay.slice(relay.indexOf('://') + 3).split(/[/?#]/, 1)[0];
  if (!url.hostname || url.username || url.password || url.hash ||
      authority.includes('@') || (url.protocol !== 'wss:' && url.protocol !== 'ws:')) {
    throw new Error('Pairing requires WSS outside loopback');
  }
  if (url.protocol === 'wss:') return;
  const hostname = url.hostname.toLowerCase();
  const octets = hostname.split('.').map(Number);
  const loopbackV4 = octets.length === 4 && octets[0] === 127 &&
    octets.every(value => Number.isInteger(value) && value >= 0 && value <= 255);
  if (hostname.replace(/\.$/, '') !== 'localhost' &&
      hostname !== '[::1]' && hostname !== '::1' && !loopbackV4) {
    throw new Error('Pairing requires WSS outside loopback');
  }
}

function encode(bytes: Uint8Array): string {
  let result = '';
  for (let i = 0; i < bytes.length; i += 3) {
    const first = bytes[i];
    const second = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const third = i + 2 < bytes.length ? bytes[i + 2] : 0;
    result += alphabet[first >> 2];
    result += alphabet[((first & 3) << 4) | (second >> 4)];
    if (i + 1 < bytes.length) result += alphabet[((second & 15) << 2) | (third >> 6)];
    if (i + 2 < bytes.length) result += alphabet[third & 63];
  }
  return result;
}

function decode(value: string): Uint8Array {
  if (!value || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
    throw new Error('Invalid base64url value');
  }
  const result: number[] = [];
  for (let i = 0; i < value.length; i += 4) {
    const block = value.slice(i, i + 4).split('').map(char => alphabet.indexOf(char));
    const first = block[0], second = block[1];
    result.push((first << 2) | (second >> 4));
    if (block.length > 2) result.push(((second & 15) << 4) | (block[2] >> 2));
    if (block.length > 3) result.push(((block[2] & 3) << 6) | block[3]);
  }
  return Uint8Array.from(result);
}

export function parsePairingUri(uri: string): Pairing {
  const url = new URL(uri.trim());
  if (url.protocol !== 'gh0st:' || url.hostname !== 'pair') {
    throw new Error('Invalid gh0st pairing URI');
  }
  const relay = url.searchParams.get('relay') || '';
  const device = url.searchParams.get('device') || '';
  const pair = url.searchParams.get('pair') || '';
  const secret = url.searchParams.get('secret') || '';
  validateRelayUrl(relay);
  if (!pairFormat.test(pair) || decode(secret).length !== 32 || !device) {
    throw new Error('Invalid pairing ID, secret, or device');
  }
  return { relay, device, pair, secret };
}

export class PairCipher {
  private senderKey: Uint8Array;
  private receiverKey: Uint8Array;
  private seenNonces = new Set<string>();
  private sentMessages = 0;
  private peer: 'device' | 'client';

  constructor(private pairing: Pairing, private role: 'device' | 'client') {
    const secret = decode(pairing.secret);
    if (secret.length !== 32 || !pairFormat.test(pairing.pair)) {
      throw new Error('Invalid pairing secret or ID');
    }
    this.peer = role === 'device' ? 'client' : 'device';
    const derive = (sender: 'device' | 'client') => hkdf(
      sha256, secret, utf8ToBytes(pairing.pair),
      utf8ToBytes(`gh0st-relay-v1:${sender}`), 32,
    );
    this.senderKey = derive(role);
    this.receiverKey = derive(this.peer);
  }

  private aad(sender: 'device' | 'client'): Uint8Array {
    return utf8ToBytes(`gh0st-relay-v1:${this.pairing.pair}:${sender}`);
  }

  async encrypt(payload: Record<string, unknown>,
                randomBytes: (length: number) => Promise<Uint8Array>): Promise<Envelope> {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload) ||
        (Object.getPrototypeOf(payload) !== Object.prototype && Object.getPrototypeOf(payload) !== null)) {
      throw new Error('Encrypted payload must be a JSON object');
    }
    if (this.sentMessages >= maxMessages) throw new Error('Pair again to send more messages');
    const plaintext = utf8ToBytes(JSON.stringify(payload));
    if (plaintext.length > 32768) throw new Error('Message exceeds 32 KiB');
    const nonce = await randomBytes(12);
    if (nonce.length !== 12) throw new Error('Random source returned an invalid nonce');
    const ciphertext = gcm(this.senderKey, nonce, this.aad(this.role)).encrypt(plaintext);
    this.sentMessages += 1;
    return { message_type: 'ciphertext', pair_id: this.pairing.pair,
             nonce: encode(nonce), ciphertext: encode(ciphertext) };
  }

  decrypt(envelope: Envelope): Record<string, unknown> {
    if (!envelope || Object.keys(envelope).sort().join(',') !== 'ciphertext,message_type,nonce,pair_id' ||
        envelope.message_type !== 'ciphertext' || envelope.pair_id !== this.pairing.pair) {
      throw new Error('Invalid ciphertext envelope');
    }
    if (this.seenNonces.size >= maxMessages) throw new Error('Pair again to receive more messages');
    const nonce = decode(envelope.nonce);
    if (nonce.length !== 12 || this.seenNonces.has(envelope.nonce)) {
      throw new Error('Invalid or replayed nonce');
    }
    const plaintext = gcm(this.receiverKey, nonce, this.aad(this.peer)).decrypt(decode(envelope.ciphertext));
    const payload = JSON.parse(bytesToUtf8(plaintext));
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('Invalid decrypted payload');
    }
    this.seenNonces.add(envelope.nonce);
    return payload;
  }
}
