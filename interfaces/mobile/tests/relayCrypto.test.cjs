const assert = require('node:assert/strict');
const vector = require('./relay-vector.json');
const { PairCipher, parsePairingUri } = require('../.test-build/relayCrypto.js');

async function main() {
  const client = new PairCipher(vector.pairing, 'client');
  const randomBytes = async length => Uint8Array.from({ length }, (_, index) => index);
  const envelope = await client.encrypt(vector.payload, async () => Uint8Array.from(
    { length: 12 }, (_, index) => index,
  ));
  assert.deepEqual(envelope, vector.envelope);
  const device = new PairCipher(vector.pairing, 'device');
  assert.deepEqual(device.decrypt(envelope), vector.payload);
  assert.throws(() => device.decrypt(envelope), /replayed/);
  assert.throws(() => new PairCipher(vector.pairing, 'client').decrypt(envelope));
  const firstCipherChar = envelope.ciphertext[0] === 'A' ? 'B' : 'A';
  const tampered = { ...envelope, ciphertext: `${firstCipherChar}${envelope.ciphertext.slice(1)}` };
  assert.throws(() => new PairCipher(vector.pairing, 'device').decrypt(tampered));
  await assert.rejects(client.encrypt(null, randomBytes), /JSON object/);
  await assert.rejects(client.encrypt([], randomBytes), /JSON object/);
  const uri = 'gh0st://pair?' + new URLSearchParams({
    relay: vector.pairing.relay, device: vector.pairing.device,
    pair: vector.pairing.pair, secret: vector.pairing.secret,
  });
  assert.deepEqual(parsePairingUri(uri), vector.pairing);
  const pairingUri = relay => 'gh0st://pair?' + new URLSearchParams({
    ...vector.pairing, relay,
  });
  for (const relay of ['ws://localhost:8765', 'ws://127.0.0.1:8765', 'ws://[::1]:8765']) {
    assert.equal(parsePairingUri(pairingUri(relay)).relay, relay);
  }
  for (const relay of [
    'ws://127.0.0.1:80@evil.test',
    'ws://evil.test',
    'https://relay.example.test',
    'wss://@relay.example.test',
  ]) {
    assert.throws(() => parsePairingUri(pairingUri(relay)));
  }
  console.log('Relay crypto vector passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
