const assert = require('node:assert/strict');
const vector = require('./relay-vector.json');
const { PairCipher, parsePairingUri } = require('../.test-build/relayCrypto.js');

async function main() {
  const client = new PairCipher(vector.pairing, 'client');
  const envelope = await client.encrypt(vector.payload, async () => Uint8Array.from(
    { length: 12 }, (_, index) => index,
  ));
  assert.deepEqual(envelope, vector.envelope);
  const device = new PairCipher(vector.pairing, 'device');
  assert.deepEqual(device.decrypt(envelope), vector.payload);
  assert.throws(() => device.decrypt(envelope), /replayed/);
  const uri = 'gh0st://pair?' + new URLSearchParams({
    relay: vector.pairing.relay, device: vector.pairing.device,
    pair: vector.pairing.pair, secret: vector.pairing.secret,
  });
  assert.deepEqual(parsePairingUri(uri), vector.pairing);
  console.log('Relay crypto vector passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
