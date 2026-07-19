(() => {
  "use strict";

  /**
   * Determine whether a value is a non-null object rather than an array.
   *
   * @param {*} value Value to inspect.
   * @returns {value is Object<string, *>} Whether the value is an object.
   */
  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  /**
   * Parse a JSON string or validate an already-decoded object.
   *
   * @param {string|Object<string, *>} value Encoded or decoded object.
   * @param {string} name Human-readable input name used in validation errors.
   * @returns {Object<string, *>} The decoded object.
   * @throws {TypeError} If the string is invalid JSON or the result is not an object.
   */
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

  /**
   * Browser-side client shared by a Stream Dock Property Inspector.
   *
   * Stream Dock initializes the singleton through
   * {@link window.connectElgatoStreamDeckSocket}. Consumers normally use the
   * {@link window.MiraBoxPropertyInspector} instance instead of constructing a
   * client. Messages sent while the WebSocket is connecting are queued and
   * flushed after registration.
   */
  class MiraBoxPropertyInspectorClient {
    /** Initialize disconnected client state and an empty settings snapshot. */
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

    /**
     * Manifest UUID of the action edited by this Property Inspector.
     *
     * @returns {string|undefined} Action UUID, or `undefined` before connection setup.
     */
    get action() {
      return this._action;
    }

    /**
     * Opaque action-context identifier supplied by Stream Dock.
     *
     * @returns {string|undefined} Context ID, or `undefined` before connection setup.
     */
    get context() {
      return this._context;
    }

    /**
     * Return a shallow snapshot of the latest action settings.
     *
     * The snapshot is initialized from `actionInfo` and refreshed on every
     * `didReceiveSettings` message or local settings update.
     *
     * @returns {Object<string, *>} Current settings snapshot.
     */
    get settings() {
      return { ...this._settings };
    }

    /**
     * Registration metadata supplied to the Property Inspector callback.
     *
     * @returns {Object<string, *>} Parsed Stream Dock information object.
     */
    get info() {
      return this._info;
    }

    /**
     * Metadata describing the action instance and its initial payload.
     *
     * @returns {Object<string, *>} Parsed action information object.
     */
    get actionInfo() {
      return this._actionInfo;
    }

    /**
     * Whether the Property Inspector WebSocket is currently open.
     *
     * @returns {boolean} `true` only while the socket is in `WebSocket.OPEN` state.
     */
    get isConnected() {
      return this._websocket?.readyState === WebSocket.OPEN;
    }

    /**
     * Subscribe to a client lifecycle or Stream Dock protocol event.
     *
     * Built-in lifecycle names are `connected`, `disconnected`, `error`,
     * `protocolError`, and `message`. Every incoming message with a string
     * `event` field is also emitted under that field's value, for example
     * `didReceiveSettings`.
     *
     * @param {string} eventName Non-empty lifecycle or wire event name.
     * @param {function(*): void} listener Callback receiving the event payload.
     * @returns {function(): void} Function that removes this exact subscription.
     * @throws {TypeError} If the event name is empty or the listener is not a function.
     */
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

    /**
     * Remove a previously registered event listener.
     *
     * Unknown event names and already-removed listeners are ignored.
     *
     * @param {string} eventName Event name originally passed to {@link on}.
     * @param {function(*): void} listener Exact callback originally registered.
     * @returns {void}
     */
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

    /**
     * Validate host callback data, open the WebSocket, and register the inspector.
     *
     * This method is called by {@link window.connectElgatoStreamDeckSocket};
     * Property Inspector application code rarely needs to call it directly.
     * The `connected` event fires after registration and queued messages have
     * been sent.
     *
     * @param {number|string} port Loopback WebSocket port from 1 through 65535.
     * @param {string} propertyInspectorUUID Opaque context used for registration.
     * @param {string} registerEvent Runtime-provided registration event name.
     * @param {string|Object<string, *>} info Host registration metadata.
     * @param {string|Object<string, *>} actionInfo Action identity and initial settings.
     * @returns {void}
     * @throws {TypeError} If any launch value violates the expected contract.
     */
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

    /**
     * Send a raw protocol message or queue it while the socket is connecting.
     *
     * Messages submitted after the socket has closed are not retained.
     * Prefer the typed convenience methods for ordinary plugin communication.
     *
     * @param {Object<string, *>} message JSON-compatible protocol envelope.
     * @returns {boolean} `true` if sent immediately; `false` if queued or not sent.
     * @throws {TypeError} If `message` is not an object.
     */
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

    /**
     * Send a plugin-defined object to the active Python action context.
     *
     * @param {Object<string, *>} payload Plugin-defined message body.
     * @returns {boolean} `true` if sent immediately; `false` if queued or not sent.
     * @throws {TypeError} If `payload` is not an object.
     */
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

    /**
     * Replace and persist all settings for the active action context.
     *
     * The local settings snapshot changes before the message is sent or queued.
     *
     * @param {Object<string, *>} settings Complete new settings object.
     * @returns {boolean} `true` if sent immediately; `false` if queued or not sent.
     * @throws {TypeError} If `settings` is not an object.
     */
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

    /**
     * Shallow-merge selected fields into the current settings and persist them.
     *
     * @param {Object<string, *>} patch Top-level setting fields to add or replace.
     * @returns {boolean} `true` if sent immediately; `false` if queued or not sent.
     * @throws {TypeError} If `patch` is not an object.
     */
    updateSettings(patch) {
      if (!isObject(patch)) {
        throw new TypeError("settings patch must be an object");
      }
      return this.setSettings({ ...this._settings, ...patch });
    }

    /**
     * Request the latest persisted settings for the active action context.
     *
     * Listen for `didReceiveSettings` to observe the asynchronous response.
     *
     * @returns {boolean} `true` if sent immediately; `false` if queued or not sent.
     */
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

  /** @type {MiraBoxPropertyInspectorClient} */
  const client = new MiraBoxPropertyInspectorClient();

  /**
   * Shared high-level Property Inspector client.
   * @type {MiraBoxPropertyInspectorClient}
   */
  window.MiraBoxPropertyInspector = client;

  /**
   * Stream Dock compatibility callback that initializes the shared client.
   *
   * @param {number|string} port Loopback WebSocket port.
   * @param {string} propertyInspectorUUID Opaque Property Inspector context.
   * @param {string} registerEvent Runtime-provided registration event.
   * @param {string|Object<string, *>} info Host registration metadata.
   * @param {string|Object<string, *>} actionInfo Action identity and settings.
   * @returns {void}
   */
  window.connectElgatoStreamDeckSocket = (...args) => client.connect(...args);
})();
