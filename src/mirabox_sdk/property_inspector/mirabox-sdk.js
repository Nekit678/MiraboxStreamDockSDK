(() => {
  "use strict";

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function parseObject(value, name) {
    let parsed = value;
    if (typeof value === "string") {
      try {
        parsed = JSON.parse(value);
      } catch {
        throw new TypeError(`${name} must contain valid JSON`);
      }
    }
    if (!isObject(parsed)) {
      throw new TypeError(`${name} must be an object`);
    }
    return parsed;
  }

  class MiraBoxPropertyInspectorClient {
    constructor() {
      this._listeners = new Map();
      this._pendingMessages = [];
      this._websocket = undefined;
      this._action = undefined;
      this._context = undefined;
      this._settings = {};
      this._info = {};
      this._actionInfo = {};
    }

    get action() {
      return this._action;
    }

    get context() {
      return this._context;
    }

    get settings() {
      return { ...this._settings };
    }

    get info() {
      return this._info;
    }

    get actionInfo() {
      return this._actionInfo;
    }

    get isConnected() {
      return this._websocket?.readyState === WebSocket.OPEN;
    }

    on(eventName, listener) {
      if (typeof eventName !== "string" || eventName.length === 0) {
        throw new TypeError("eventName must be a non-empty string");
      }
      if (typeof listener !== "function") {
        throw new TypeError("listener must be a function");
      }

      let listeners = this._listeners.get(eventName);
      if (listeners === undefined) {
        listeners = new Set();
        this._listeners.set(eventName, listeners);
      }
      listeners.add(listener);
      return () => this.off(eventName, listener);
    }

    off(eventName, listener) {
      const listeners = this._listeners.get(eventName);
      if (listeners === undefined) {
        return;
      }
      listeners.delete(listener);
      if (listeners.size === 0) {
        this._listeners.delete(eventName);
      }
    }

    connect(port, propertyInspectorUUID, registerEvent, info, actionInfo) {
      const portNumber = Number(port);
      if (!Number.isInteger(portNumber) || portNumber < 1 || portNumber > 65535) {
        throw new TypeError("port must be an integer from 1 to 65535");
      }
      if (typeof propertyInspectorUUID !== "string" || propertyInspectorUUID.length === 0) {
        throw new TypeError("propertyInspectorUUID must be a non-empty string");
      }
      if (typeof registerEvent !== "string" || registerEvent.length === 0) {
        throw new TypeError("registerEvent must be a non-empty string");
      }

      this._info = parseObject(info, "info");
      this._actionInfo = parseObject(actionInfo, "actionInfo");
      this._action = this._actionInfo.action;
      if (typeof this._action !== "string" || this._action.length === 0) {
        throw new TypeError("actionInfo.action must be a non-empty string");
      }
      this._context = propertyInspectorUUID;
      this._settings = isObject(this._actionInfo.payload?.settings)
        ? { ...this._actionInfo.payload.settings }
        : {};

      const websocket = new WebSocket(`ws://127.0.0.1:${portNumber}`);
      this._websocket = websocket;
      websocket.addEventListener("open", () => {
        this.send({ event: registerEvent, uuid: propertyInspectorUUID });
        const pendingMessages = this._pendingMessages.splice(0);
        for (const message of pendingMessages) {
          this.send(message);
        }
        this._emit("connected", {
          action: this._action,
          context: this._context,
          info: this._info,
          actionInfo: this._actionInfo,
          settings: this.settings,
        });
      });
      websocket.addEventListener("message", (event) => this._receive(event));
      websocket.addEventListener("error", (event) => this._emit("error", event));
      websocket.addEventListener("close", (event) => this._emit("disconnected", event));
    }

    send(message) {
      if (!isObject(message)) {
        throw new TypeError("message must be an object");
      }
      if (this._websocket?.readyState === WebSocket.OPEN) {
        this._websocket.send(JSON.stringify(message));
        return true;
      }
      if (this._websocket?.readyState === WebSocket.CONNECTING) {
        this._pendingMessages.push(message);
      }
      return false;
    }

    sendToPlugin(payload) {
      if (!isObject(payload)) {
        throw new TypeError("payload must be an object");
      }
      return this.send({
        event: "sendToPlugin",
        action: this._action,
        context: this._context,
        payload,
      });
    }

    setSettings(settings) {
      if (!isObject(settings)) {
        throw new TypeError("settings must be an object");
      }
      this._settings = { ...settings };
      return this.send({
        event: "setSettings",
        context: this._context,
        payload: this._settings,
      });
    }

    updateSettings(patch) {
      if (!isObject(patch)) {
        throw new TypeError("settings patch must be an object");
      }
      return this.setSettings({ ...this._settings, ...patch });
    }

    getSettings() {
      return this.send({ event: "getSettings", context: this._context });
    }

    _receive(event) {
      let message;
      try {
        message = parseObject(event.data, "WebSocket message");
      } catch (error) {
        console.error("Ignoring invalid Stream Dock message", error);
        this._emit("protocolError", { error, data: event.data });
        return;
      }

      if (message.event === "didReceiveSettings") {
        this._settings = isObject(message.payload?.settings)
          ? { ...message.payload.settings }
          : {};
      }
      if (typeof message.event === "string") {
        this._emit(message.event, message);
      }
      this._emit("message", message);
    }

    _emit(eventName, payload) {
      const listeners = this._listeners.get(eventName);
      if (listeners === undefined) {
        return;
      }
      for (const listener of [...listeners]) {
        try {
          listener(payload);
        } catch (error) {
          console.error(`Property Inspector listener failed for ${eventName}`, error);
        }
      }
    }
  }

  const client = new MiraBoxPropertyInspectorClient();
  window.MiraBoxPropertyInspector = client;
  window.connectElgatoStreamDeckSocket = (...args) => client.connect(...args);
})();
