'use strict';

const { gunzipSync } = require('node:zlib');
const ENDPOINT = 'wss://open-api-swap.bingx.com/swap-market';
const TOPIC = 'BTC-USDT@lastPrice';
const MAX_FRAME = 65536;
const MAX_AGE_MS = 15000;

function lastPrice(text, now) {
  const message = JSON.parse(text);
  if (!message || message.dataType !== TOPIC || message.data?.s !== 'BTC-USDT') return null;
  if (message.code !== undefined && message.code !== 0) return null;
  if (message.data.e !== undefined && message.data.e !== 'lastPriceUpdate') return null;
  const { c, E } = message.data;
  const price = typeof c === 'number' || typeof c === 'string' ? String(c) : '';
  if (!/^\d{1,20}(?:\.\d{1,12})?$/.test(price) || Number(price) <= 0) return null;
  if (!Number.isSafeInteger(E) || E <= 0 || now - E > MAX_AGE_MS || E - now > 2000) return null;
  return { symbol: 'BTC-USDT', price, event_ms: E };
}

function receive(WebSocket, emit, finish) {
  const socket = new WebSocket(ENDPOINT);
  socket.binaryType = 'arraybuffer';
  let previousEvent = 0;
  let finished = false;
  let timer = setTimeout(stop, MAX_AGE_MS);

  function stop() {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    socket.close();
    finish();
  }

  socket.addEventListener('open', () => {
    socket.send(JSON.stringify({ id: 'sina-btcusdt-last', reqType: 'sub', dataType: TOPIC }));
  });
  socket.addEventListener('error', stop);
  socket.addEventListener('close', stop);
  socket.addEventListener('message', ({ data }) => {
    if (finished) return;
    try {
      let message = data;
      if (typeof data !== 'string') {
        if (data.byteLength > MAX_FRAME) return;
        message = gunzipSync(Buffer.from(data), { maxOutputLength: MAX_FRAME }).toString('utf8');
      }
      if (message.length > MAX_FRAME) return;
      if (message === 'Ping') {
        socket.send('Pong');
        return;
      }
      const quote = lastPrice(message, Date.now());
      if (!quote || quote.event_ms <= previousEvent) return;
      previousEvent = quote.event_ms;
      emit(quote);
      clearTimeout(timer);
      timer = setTimeout(stop, MAX_AGE_MS);
    } catch {
      // A malformed frame cannot advance freshness or the receive timeout.
    }
  });
  return stop;
}

module.exports = { lastPrice, receive };
if (require.main === module) {
  receive(WebSocket, quote => process.stdout.write(JSON.stringify(quote) + '\n'),
    () => process.exit(1));
}
