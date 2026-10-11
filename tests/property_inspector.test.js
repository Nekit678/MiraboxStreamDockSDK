"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const { createContext, runInContext } = require("node:vm");

const root = resolve(__dirname, "..");
const sourcePath = "src/mirabox_sdk/property_inspector/mirabox-sdk.js";
const bundlePath = "examples/counter_plugin/com.example.counter.sdPlugin/property-inspector";
const read = (path) => readFileSync(resolve(root, path), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));

function createInspector() {
  const sockets = [];
  class WebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = WebSocket.CONNECTING;
      this.listeners = new Map();
      this.frames = [];
      this.bufferedAmount = 0;
      this.peakBufferedAmount = 0;
      this.beforeSend = () => {};
      sockets.push(this);
    }

    addEventListener(name, listener) {
      const listeners = this.listeners.get(name) ?? [];
      listeners.push(listener);
      this.listeners.set(name, listeners);
    }

    emit(name, event = {}) {
      for (const listener of this.listeners.get(name) ?? []) {
        listener(event);
      }
    }

    open() {
      this.readyState = WebSocket.OPEN;
      this.emit("open");
    }

    close() {
      this.readyState = WebSocket.CLOSED;
      this.emit("close", { code: 1000 });
    }

    drain(bytes = this.bufferedAmount) {
      this.bufferedAmount = Math.max(0, this.bufferedAmount - bytes);
    }

    send(data) {
      this.beforeSend(JSON.parse(data));
      if (this.readyState !== WebSocket.OPEN) {
        throw new Error("Socket is closed");
      }
      this.frames.push(JSON.parse(data));
      this.bufferedAmount += Buffer.byteLength(data, "utf8");
      this.peakBufferedAmount = Math.max(this.peakBufferedAmount, this.bufferedAmount);
    }
  }

  const context = createContext({
    window: {},
    WebSocket,
    console: { log() {}, error() {} },
  });
  runInContext(read(sourcePath), context, { filename: sourcePath });
  const client = context.window.MiraBoxPropertyInspector;
  const connect = (settings = {}, uuid = "pi-context", action = "example.action") => {
    context.window.connectElgatoStreamDeckSocket(
      "12345", uuid, "registerPropertyInspector", "{}",
      { action, payload: { settings } },
    );
    return sockets.at(-1);
  };
  return { client, connect, context, WebSocket };
}

const operations = {
  send: (client) => client.send({ event: "custom" }),
  sendToPlugin: (client) => client.sendToPlugin({ value: 1 }),
  setSettings: (client) => client.setSettings({ mode: "new" }),
  updateSettings: (client) => client.updateSettings({ mode: "new" }),
  getSettings: (client) => client.getSettings(),
};

test("an old socket closing cannot reject the new session's queued message", () => {
  const { client, connect, WebSocket } = createInspector();
  const first = connect();
  first.open();
  const errors = [];
  const disconnected = [];
  client.on("sendError", (event) => errors.push(event));
  client.on("disconnected", (event) => disconnected.push(event));
  const second = connect({}, "new-context", "new.action");
  assert.equal(client.sendToPlugin({ value: "new" }), false);
  const bytes = client.queueMetrics.currentBytes;
  first.close();
  assert.equal(second.readyState, WebSocket.CONNECTING);
  assert.equal(client.queueMetrics.currentBytes, bytes);
  assert.equal(client.queueMetrics.currentDepth, 1);
  assert.deepEqual(errors, []);
  assert.deepEqual(disconnected, []);
  second.open();
  assert.deepEqual(second.frames, [
    { event: "registerPropertyInspector", uuid: "new-context" },
    { event: "sendToPlugin", action: "new.action", context: "new-context", payload: { value: "new" } },
  ]);
});

test("stale open, message and error events cannot affect the active session", () => {
  const { client, connect, WebSocket } = createInspector();
  const first = connect({ mode: "old" });
  const events = [];
  for (const name of ["connected", "disconnected", "message", "didReceiveSettings", "error", "protocolError", "sendError"]) {
    client.on(name, () => events.push(name));
  }
  const second = connect({ mode: "new" }, "new-context", "new.action");
  assert.equal(first.readyState, WebSocket.CLOSED);
  client.sendToPlugin({ value: "new" });
  const metrics = plain(client.queueMetrics);
  first.open();
  first.emit("message", {
    data: JSON.stringify({ event: "didReceiveSettings", payload: { settings: { mode: "stale" } } }),
  });
  first.emit("message", { data: "invalid JSON" });
  first.emit("error", { message: "old error" });
  first.close();
  assert.deepEqual(first.frames, []);
  assert.deepEqual(plain(client.settings), { mode: "new" });
  assert.deepEqual(plain(client.queueMetrics), metrics);
  assert.deepEqual(events, []);
  second.open();
  assert.deepEqual(events, ["connected"]);
  assert.deepEqual(second.frames[1].payload, { value: "new" });
});

test("replacement fails old pending messages once and preserves sends from sendError listeners", () => {
  const { client, connect, WebSocket } = createInspector();
  const first = connect();
  client.sendToPlugin({ value: "old" });
  const errors = [];
  client.on("sendError", (event) => {
    errors.push(event);
    client.sendToPlugin({ value: "new" });
  });
  const second = connect({}, "new-context", "new.action");
  assert.equal(first.readyState, WebSocket.CLOSED);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].message.context, "pi-context");
  assert.match(errors[0].error.message, /replaced/);
  assert.equal(client.queueMetrics.currentDepth, 1);
  first.close();
  second.open();
  assert.equal(errors.length, 1);
  assert.deepEqual(second.frames[1], {
    event: "sendToPlugin", action: "new.action", context: "new-context", payload: { value: "new" },
  });
});

test("reconnecting from a deferred failure cannot flush or reject the new session's queue", () => {
  const { client, connect } = createInspector();
  const first = connect();
  client.sendToPlugin({ value: 1 });
  client.sendToPlugin({ value: 2 });
  first.beforeSend = (message) => {
    if (message.payload?.value === 1) throw new Error("send failed");
  };
  let second;
  const errors = [];
  const connected = [];
  client.on("sendError", (event) => {
    errors.push(event);
    if (second === undefined) {
      second = connect({}, "new-context");
      client.sendToPlugin({ value: "new" });
    }
  });
  client.on("connected", (event) => connected.push(event.context));
  first.open();
  assert.deepEqual(errors.map(({ message }) => message.payload.value), [1, 2]);
  assert.equal(first.frames.length, 1);
  assert.deepEqual(connected, []);
  assert.equal(client.queueMetrics.currentDepth, 1);
  second.open();
  assert.deepEqual(connected, ["new-context"]);
  assert.deepEqual(second.frames[1].payload, { value: "new" });
});

test("reconnecting from registration sendError cannot reject new pending messages", () => {
  const { client, connect } = createInspector();
  const first = connect();
  first.beforeSend = () => { throw new Error("registration failed"); };
  let second;
  const errors = [];
  client.on("sendError", (event) => {
    errors.push(event);
    second = connect({}, "new-context");
    client.sendToPlugin({ value: "new" });
  });
  first.open();
  assert.equal(errors.length, 1);
  assert.equal(client.queueMetrics.currentDepth, 1);
  second.open();
  assert.deepEqual(second.frames[1].payload, { value: "new" });
});

test("reconnecting during a settings event stops further dispatch from the old socket", () => {
  const { client, connect } = createInspector();
  const first = connect();
  first.open();
  const events = [];
  client.on("didReceiveSettings", () => {
    events.push("first listener");
    connect({ mode: "new" }, "new-context");
  });
  client.on("didReceiveSettings", () => events.push("second listener"));
  client.on("message", () => events.push("message"));
  client.on("disconnected", () => events.push("disconnected"));
  first.emit("message", {
    data: JSON.stringify({ event: "didReceiveSettings", payload: { settings: { mode: "old" } } }),
  });
  assert.deepEqual(events, ["first listener"]);
  assert.deepEqual(plain(client.settings), { mode: "new" });
});

test("invalid reconnect metadata preserves the active connection and queue", () => {
  const { client, connect, WebSocket } = createInspector();
  const socket = connect({ mode: "old" });
  client.sendToPlugin({ value: "old" });
  const metrics = plain(client.queueMetrics);
  for (const [info, actionInfo] of [
    ["invalid JSON", { action: "new.action" }],
    [{ host: "new" }, { action: "" }],
    [{ host: "new" }, { action: "new.action", payload: { settings: { value: 1n } } }],
  ]) {
    assert.throws(() => client.connect("12345", "new-context", "registerPropertyInspector", info, actionInfo));
    assert.equal(socket.readyState, WebSocket.CONNECTING);
    assert.equal(client.action, "example.action");
    assert.equal(client.context, "pi-context");
    assert.deepEqual(plain(client.info), {});
    assert.deepEqual(plain(client.actionInfo), { action: "example.action", payload: { settings: { mode: "old" } } });
    assert.deepEqual(plain(client.settings), { mode: "old" });
    assert.deepEqual(plain(client.queueMetrics), metrics);
  }
  socket.open();
  assert.deepEqual(socket.frames[1].payload, { value: "old" });
});

test("a non-draining OPEN socket bounds a 512-message burst of 64 KiB payloads", () => {
  const { client, connect } = createInspector();
  const socket = connect({ mode: "old" });
  socket.open();
  const payload = { text: "é".repeat(32 * 1024) };
  let accepted = 0;
  let rejected = 0;
  for (let index = 0; index < 512; index += 1) {
    try {
      assert.equal(client.sendToPlugin(payload), true);
      accepted += 1;
    } catch (error) {
      assert.equal(error.name, "RangeError");
      assert.match(error.message, /buffer is full/);
      rejected += 1;
    }
  }
  const metrics = client.queueMetrics;
  assert.ok(accepted > 0 && accepted < 512);
  assert.equal(rejected, 512 - accepted);
  assert.equal(metrics.rejectedFull, rejected);
  assert.equal(metrics.currentBytes, 0);
  assert.equal(metrics.currentDepth, 0);
  assert.equal(metrics.bufferedBytes, socket.bufferedAmount);
  assert.ok(socket.peakBufferedAmount <= metrics.byteLimit);
  assert.throws(() => client.setSettings(payload), /buffer is full/);
  assert.deepEqual(plain(client.settings), { mode: "old" });
  socket.drain();
  assert.equal(client.queueMetrics.bufferedBytes, 0);
  assert.equal(client.sendToPlugin(payload), true);
});

test("the OPEN byte budget accepts its boundary and recovers after a partial drain", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  socket.open();
  const message = { event: "custom", text: "😀" };
  const size = Buffer.byteLength(JSON.stringify(message), "utf8");
  socket.bufferedAmount = client.queueMetrics.byteLimit - size;
  assert.equal(client.send(message), true);
  assert.equal(socket.bufferedAmount, client.queueMetrics.byteLimit);
  assert.throws(() => client.send(message), /buffer is full/);
  socket.drain(size - 1);
  assert.throws(() => client.send(message), /buffer is full/);
  socket.drain(1);
  assert.equal(client.send(message), true);
  assert.equal(socket.bufferedAmount, client.queueMetrics.byteLimit);
  assert.equal(client.queueMetrics.rejectedFull, 2);
});

test("a full connecting backlog cannot overflow the OPEN buffer during flush", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  const message = { event: "custom", text: "" };
  const overhead = Buffer.byteLength(JSON.stringify(message), "utf8");
  message.text = "x".repeat(client.queueMetrics.maxMessageBytes - overhead);
  client.send(message);
  client.send(message);
  assert.equal(client.queueMetrics.currentBytes, client.queueMetrics.byteLimit);
  const errors = [];
  let connected = false;
  client.on("sendError", (event) => errors.push(event));
  client.on("connected", () => { connected = true; });
  socket.open();
  assert.ok(socket.peakBufferedAmount <= client.queueMetrics.byteLimit);
  assert.equal(socket.frames.length, 2);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].message.event, "custom");
  assert.equal(errors[0].error.name, "RangeError");
  assert.equal(client.queueMetrics.rejectedFull, 1);
  assert.equal(client.queueMetrics.currentBytes, 0);
  assert.equal(client.queueMetrics.currentDepth, 0);
  assert.equal(connected, true);
  socket.close();
  assert.equal(errors.length, 1);
  assert.throws(() => client.send({ event: "custom" }), /closing or closed/);
});

test("buffer pressure during flush reports the rejected message and attempts later messages", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  const message = { event: "custom", text: "" };
  const overhead = Buffer.byteLength(JSON.stringify(message), "utf8");
  message.text = "x".repeat(client.queueMetrics.maxMessageBytes - overhead);
  client.send(message);
  message.text = message.text.slice(16);
  client.send(message);
  client.send({ event: "last" });
  const errors = [];
  client.on("sendError", (event) => errors.push(event));
  socket.open();
  assert.deepEqual(socket.frames.map(({ event }) => event), ["registerPropertyInspector", "custom", "last"]);
  assert.equal(errors.length, 1);
  assert.equal(errors[0].message.text.length, message.text.length);
  assert.match(errors[0].error.message, /buffer is full/);
  assert.ok(socket.peakBufferedAmount <= client.queueMetrics.byteLimit);
  assert.equal(client.queueMetrics.currentBytes, 0);
});

test("registration also respects the buffer budget and rejects pending messages on failure", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  client.sendToPlugin({ value: 1 });
  socket.bufferedAmount = client.queueMetrics.byteLimit;
  const errors = [];
  let connected = false;
  client.on("sendError", (event) => errors.push(event));
  client.on("connected", () => { connected = true; });
  socket.open();
  assert.equal(socket.frames.length, 0);
  assert.equal(errors.length, 2);
  assert.ok(errors.some(({ message, error }) => message.event === "registerPropertyInspector" && error.name === "RangeError"));
  assert.equal(client.queueMetrics.currentBytes, 0);
  assert.equal(client.queueMetrics.currentDepth, 0);
  assert.equal(client.queueMetrics.rejectedFull, 1);
  assert.equal(connected, false);
  assert.equal(client.isConnected, false);
});

test("a socket stuck in CONNECTING explicitly rejects a 10,000 message burst", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  const errors = [];
  client.on("sendError", (event) => {
    errors.push(event);
    assert.equal(client.queueMetrics.currentBytes, 0);
  });
  let accepted = 0;
  let rejected = 0;
  for (let index = 0; index < 10000; index += 1) {
    try {
      assert.equal(client.sendToPlugin({ index }), false);
      accepted += 1;
    } catch (error) {
      assert.equal(error.name, "RangeError");
      assert.match(error.message, /queue is full/);
      rejected += 1;
    }
  }
  const metrics = client.queueMetrics;
  assert.equal(accepted, 1024);
  assert.equal(rejected, 8976);
  assert.equal(metrics.currentDepth, accepted);
  assert.equal(metrics.rejectedFull, rejected);
  assert.ok(metrics.currentBytes <= metrics.byteLimit);
  socket.close();
  assert.equal(errors.length, accepted);
  assert.deepEqual(errors.map(({ message }) => message.payload.index),
    Array.from({ length: accepted }, (_, index) => index));
  assert.equal(client.queueMetrics.currentDepth, 0);
  assert.equal(client.queueMetrics.currentBytes, 0);
});

test("UTF-8 byte pressure bounds large queued payloads and preserves settings on rejection", () => {
  const { client, connect } = createInspector();
  const socket = connect({ mode: "old" });
  const payload = { text: "é".repeat(512 * 1024) };
  const message = {
    event: "sendToPlugin", action: "example.action", context: "pi-context", payload,
  };
  const size = Buffer.byteLength(JSON.stringify(message), "utf8");
  const capacity = Math.floor(client.queueMetrics.byteLimit / size);
  for (let index = 0; index < capacity; index += 1) {
    assert.equal(client.sendToPlugin(payload), false);
  }
  assert.throws(() => client.sendToPlugin(payload), /queue is full/);
  assert.throws(() => client.setSettings(payload), /queue is full/);
  assert.deepEqual(plain(client.settings), { mode: "old" });
  const metrics = client.queueMetrics;
  assert.equal(metrics.currentDepth, capacity);
  assert.ok(capacity < metrics.messageLimit);
  assert.equal(metrics.currentBytes, capacity * size);
  assert.equal(metrics.peakBytes, metrics.currentBytes);
  socket.open();
  assert.equal(socket.frames.length, capacity + 1);
  assert.equal(client.queueMetrics.currentBytes, 0);
  assert.equal(client.queueMetrics.currentDepth, 0);
});

for (const immediate of [false, true]) {
  test(`single-frame byte limit accepts its boundary and rejects oversized ${immediate ? "open" : "connecting"} sends`, () => {
    const { client, connect } = createInspector();
    const socket = connect();
    if (immediate) socket.open();
    const message = { event: "custom", text: "" };
    const overhead = Buffer.byteLength(JSON.stringify(message), "utf8");
    message.text = "x".repeat(client.queueMetrics.maxMessageBytes - overhead);
    assert.equal(client.send(message), immediate);
    message.text += "x";
    assert.throws(() => client.send(message), /message exceeds byte limit/);
    assert.equal(client.queueMetrics.rejectedOversized, 1);
    if (!immediate) socket.open();
    assert.equal(socket.frames.length, 2);
    assert.equal(client.queueMetrics.currentBytes, 0);
  });
}

test("oversized settings and inbound frames leave the settings snapshot intact", () => {
  const { client, connect } = createInspector();
  const socket = connect({ mode: "old" });
  socket.open();
  const settings = { text: "😀".repeat(client.queueMetrics.maxMessageBytes / 4) };
  assert.throws(() => client.setSettings(settings), /message exceeds byte limit/);
  assert.deepEqual(plain(client.settings), { mode: "old" });
  const errors = [];
  client.on("protocolError", (event) => errors.push(event));
  socket.emit("message", {
    data: JSON.stringify({ event: "didReceiveSettings", payload: { settings } }),
  });
  assert.equal(errors.length, 1);
  assert.equal(errors[0].error.name, "RangeError");
  assert.deepEqual(plain(client.settings), { mode: "old" });
  assert.equal(socket.frames.length, 1);
  client.queueMetrics.currentBytes = 100;
  assert.equal(client.queueMetrics.currentBytes, 0);
});

for (const [name, operation] of Object.entries(operations)) {
  test(`${name} rejects calls before the host callback`, () => {
    const { client } = createInspector();
    assert.throws(() => operation(client), /not initialized/);
    assert.deepEqual(plain(client.settings), {});
  });

  for (const state of ["CLOSING", "CLOSED"]) {
    test(`${name} rejects a ${state} socket without changing settings`, () => {
      const { client, connect, WebSocket } = createInspector();
      const socket = connect({ mode: "old" });
      socket.open();
      socket.readyState = WebSocket[state];
      assert.throws(() => operation(client), /closing or closed/);
      assert.deepEqual(plain(client.settings), { mode: "old" });
      assert.equal(socket.frames.length, 1);
    });
  }
}

test("queued and immediate sends have distinct results and valid routing", () => {
  const { client, connect } = createInspector();
  const socket = connect({ count: 3 });
  assert.equal(client.sendToPlugin({ value: 1 }), false);
  assert.equal(client.updateSettings({ mode: "toggle" }), false);
  assert.equal(client.getSettings(), false);
  const connected = [];
  client.on("connected", () => connected.push(socket.frames.length));
  socket.open();
  assert.deepEqual(socket.frames, [
    { event: "registerPropertyInspector", uuid: "pi-context" },
    { event: "sendToPlugin", action: "example.action", context: "pi-context", payload: { value: 1 } },
    { event: "setSettings", context: "pi-context", payload: { count: 3, mode: "toggle" } },
    { event: "getSettings", context: "pi-context" },
  ]);
  assert.deepEqual(connected, [4]);
  assert.equal(client.sendToPlugin({ value: 2 }), true);
});

test("queued messages own nested payloads and raw envelopes", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  const payload = { nested: { value: 1 } };
  const message = { event: "custom", payload: { items: [1] } };
  client.sendToPlugin(payload);
  client.send(message);
  payload.nested.value = 2;
  message.event = "changed";
  message.payload.items.push(2);
  socket.open();
  assert.deepEqual(socket.frames[1].payload, { nested: { value: 1 } });
  assert.deepEqual(socket.frames[2], { event: "custom", payload: { items: [1] } });
});

test("settings snapshots isolate host input, callers, getters, and wire payloads", () => {
  const { client, connect } = createInspector();
  const initial = { nested: { value: "host" } };
  const socket = connect(initial);
  initial.nested.value = "changed";
  assert.deepEqual(plain(client.settings), { nested: { value: "host" } });
  const settings = { nested: { items: [1] } };
  assert.equal(client.setSettings(settings), false);
  settings.nested.items.push(2);
  client.settings.nested.items.push(3);
  assert.deepEqual(plain(client.settings), { nested: { items: [1] } });
  socket.open();
  assert.deepEqual(socket.frames[1].payload, { nested: { items: [1] } });
  assert.equal(client.updateSettings({ other: { value: 4 } }), true);
  assert.deepEqual(plain(client.settings), { nested: { items: [1] }, other: { value: 4 } });
  socket.emit("message", {
    data: JSON.stringify({ event: "didReceiveSettings", payload: { settings: { nested: [5] } } }),
  });
  client.settings.nested.push(6);
  assert.deepEqual(plain(client.settings), { nested: [5] });
});

for (const [label, invalid] of [
  ["cyclic object", () => { const value = {}; value.self = value; return value; }],
  ["BigInt", () => ({ value: 1n })],
]) {
  for (const name of ["send", "sendToPlugin", "setSettings", "updateSettings"]) {
    for (const immediate of [false, true]) {
      test(`${name} rejects ${label} ${immediate ? "after open" : "before enqueue"}`, () => {
        const { client, connect } = createInspector();
        const socket = connect({ mode: "old" });
        if (immediate) socket.open();
        assert.throws(() => client[name](invalid()), /circular|BigInt/i);
        assert.deepEqual(plain(client.settings), { mode: "old" });
        assert.equal(client.sendToPlugin({ valid: true }), immediate);
        let connected = false;
        client.on("connected", () => { connected = true; });
        if (!immediate) {
          socket.open();
          assert.equal(connected, true);
        }
        assert.equal(socket.frames.length, 2);
        assert.deepEqual(socket.frames[1].payload, { valid: true });
      });
    }
  }
}

test("one deferred send failure reports its message and preserves later messages", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  const failure = new Error("send failed");
  socket.beforeSend = (message) => {
    if (message.payload?.value === 1) throw failure;
  };
  const errors = [];
  const connected = [];
  client.on("sendError", (event) => errors.push(event));
  client.on("connected", () => connected.push(true));
  client.sendToPlugin({ value: 1 });
  client.sendToPlugin({ value: 2 });
  socket.open();
  assert.equal(errors.length, 1);
  assert.equal(errors[0].error, failure);
  assert.deepEqual(plain(errors[0].message.payload), { value: 1 });
  assert.deepEqual(socket.frames[1].payload, { value: 2 });
  assert.deepEqual(connected, [true]);
});

test("immediate send failures propagate and leave the settings snapshot intact", () => {
  const { client, connect } = createInspector();
  const socket = connect({ mode: "old" });
  socket.open();
  const failure = new Error("send failed");
  socket.beforeSend = () => { throw failure; };
  assert.throws(() => client.setSettings({ mode: "new" }), (error) => error === failure);
  assert.deepEqual(plain(client.settings), { mode: "old" });
});

test("accepted settings remain optimistic on deferred failure until host refresh", () => {
  const { client, connect } = createInspector();
  const socket = connect({ mode: "old" });
  socket.beforeSend = (message) => {
    if (message.event === "setSettings") throw new Error("send failed");
  };
  const errors = [];
  client.on("sendError", (event) => errors.push(event));
  assert.equal(client.setSettings({ mode: "new" }), false);
  socket.open();
  assert.equal(errors.length, 1);
  assert.deepEqual(plain(client.settings), { mode: "new" });
  socket.emit("message", {
    data: JSON.stringify({ event: "didReceiveSettings", payload: { settings: { mode: "old" } } }),
  });
  assert.deepEqual(plain(client.settings), { mode: "old" });
});

for (const stage of ["before open", "during flush"]) {
  test(`closing ${stage} gives each unsent message an explicit failure`, () => {
    const { client, connect } = createInspector();
    const socket = connect();
    const errors = [];
    let connected = false;
    let disconnected = false;
    client.on("sendError", (event) => errors.push(event));
    client.on("connected", () => { connected = true; });
    client.on("disconnected", () => { disconnected = true; });
    client.sendToPlugin({ value: 1 });
    client.sendToPlugin({ value: 2 });
    if (stage === "before open") {
      socket.close();
    } else {
      socket.beforeSend = (message) => {
        if (message.payload?.value === 1) socket.close();
      };
      socket.open();
    }
    assert.deepEqual(errors.map(({ message }) => message.payload.value).sort(), [1, 2]);
    assert.equal(connected, false);
    assert.equal(disconnected, true);
    assert.equal(client.queueMetrics.currentBytes, 0);
    assert.equal(client.queueMetrics.currentDepth, 0);
  });
}

test("registration failure rejects queued messages and prevents connected", () => {
  const { client, connect } = createInspector();
  const socket = connect();
  socket.beforeSend = () => { throw new Error("registration failed"); };
  const errors = [];
  let connected = false;
  client.on("sendError", (event) => errors.push(event));
  client.on("connected", () => { connected = true; });
  client.sendToPlugin({ value: 1 });
  socket.open();
  assert.equal(connected, false);
  assert.equal(client.isConnected, false);
  assert.equal(errors.length, 2);
  assert.equal(client.queueMetrics.currentBytes, 0);
  assert.ok(errors.some(({ message }) => message.event === "registerPropertyInspector"));
  assert.deepEqual(plain(errors.find(({ message }) => message.event === "sendToPlugin").message.payload), { value: 1 });
});

for (const document of ["README.md", "README.ru.md"]) {
  test(`${document} startup script runs before the host callback without losing operations`, () => {
    const { client, connect, context } = createInspector();
    const script = [...read(document).matchAll(/^```javascript\n([\s\S]*?)^```/gm)]
      .find((match) => match[1].includes("window.MiraBoxPropertyInspector"))[1];
    runInContext(script, context, { filename: document });
    const socket = connect({ count: 3 });
    socket.open();
    assert.deepEqual(socket.frames.map(({ event }) => event), [
      "registerPropertyInspector", "sendToPlugin", "setSettings",
    ]);
    assert.equal(socket.frames[1].action, "example.action");
    assert.equal(socket.frames[1].context, "pi-context");
    assert.deepEqual(plain(client.settings), { count: 3, mode: "toggle" });
  });
}

test("Counter ships the current Property Inspector client", () => {
  assert.equal(read(`${bundlePath}/mirabox-sdk.js`), read(sourcePath));
});

test("Counter enables controls after connection and disables them after close", () => {
  const { connect, context } = createInspector();
  const reset = { disabled: true, addEventListener(name, listener) { this[name] = listener; } };
  context.document = { getElementById: () => reset };
  runInContext(read(`${bundlePath}/counter.js`), context, { filename: "counter.js" });
  assert.equal(reset.disabled, true);
  const socket = connect();
  socket.open();
  assert.equal(reset.disabled, false);
  reset.click();
  assert.deepEqual(socket.frames[1].payload, { event: "reset" });
  socket.close();
  assert.equal(reset.disabled, true);
});
